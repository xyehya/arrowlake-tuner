"""T-014 / T-021 executor tests: end-to-end on ``SimMsrPort`` + the REAL ``WriteGuard`` (fake clock, temp audit/lock files).

Rules under test: docs/tasks/T-014-controls-cli.md ("apply_profile", "Tests"), docs/tasks/T-021-align-with-xtu-measured.md (the XTU-shaped
sequence), docs/tasks/T-013-guard.md (every executed request counts toward the session limit, read-backs do not; the guard refuses,
never sleeps), docs/SAFETY.md ("Write-session protocol"). No hardware: the only port is the simulator. "Nothing sent" is asserted on the
simulator's own request log.

The default session runs the OLD SIM-ONLY defaults (``SimConfig()`` with ``reject_zero_fields``); the measured machine
(``SimConfig.measured()``) is in test_measured_apply.py.
"""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from arltuner.controls import (
    REVERT_NOTE,
    XTU_SHAPE_CAVEAT,
    NeedsHigherLimit,
    PlanRefused,
    StepKind,
    StepStatus,
    StopKind,
)
from arltuner.guard import ENV_FLAG, AuditLog, Confirmation, GuardRefused, Refusal, Request, WriteGuard, default_m1_policy
from arltuner.mailbox import VfWord
from arltuner.msr import MailboxTransport, SimConfig, SimMsrPort

from ._support import (
    E_BASELINE,
    OPERATOR,
    P4_BASELINE,
    HookPort,
    e_core,
    make_session,
    p4,
    policy_with,
    profile,
    sim_config,
)

P4_KINDS = [
    StepKind.READ_PERCORE_ENABLE,
    StepKind.READ_VF,
    StepKind.READ_FAVORED,
    StepKind.READ_FUSED,
    StepKind.READ_GRANULAR,
]


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def session(self, **kwargs):
        return make_session(self.tmp, **kwargs)


P4_KEPT = P4_BASELINE.with_(offset_raw=-31, v_target_raw=0)  # the word the planner writes for -30 mV on P4 (no OC point) and the hardware keeps


class HappyPaths(Case):
    def test_p_core_offset_minus_30_mv_full_sequence(self) -> None:
        s = self.session()
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertTrue(report.ok, report.format_report())
        self.assertIsNone(report.stopped_reason)
        self.assertEqual(report.stop_kind, StopKind.NONE)
        self.assertEqual(report.operations, 9)
        self.assertEqual(
            [r.step.kind for r in report.steps],
            [*P4_KINDS, StepKind.WRITE_PERCORE_ENABLE, StepKind.WRITE_VF, StepKind.WRITE_FAVORED, StepKind.WRITE_GRANULAR],
        )
        # the simulator's latched requests: 9 operations + 4 guard read-backs (0x14 after 0x15, 0x10 after 0x11, 0x1C after 0x1D, 0x26 after 0x27)
        self.assertEqual(
            [(c, cmd) for c, cmd, _ in s.requests()],
            [(4, 0x14), (4, 0x10), (4, 0x1C), (4, 0x09), (4, 0x26), (4, 0x15), (4, 0x14), (4, 0x11), (4, 0x10), (4, 0x1D), (4, 0x1C), (4, 0x27), (4, 0x26)],
        )
        # the word in the simulator is the baseline with only the offset changed; 0x1D = its ratio, 0x27 = the 0x26 word
        self.assertEqual(s.sim.vf(4), P4_KEPT)  # G-120: ratio 55 <= 57 = no OC point: ratio | offset << 21, V target 0
        self.assertEqual((s.sim.vf(4).ratio, s.sim.vf(4).v_target_raw, s.sim.vf(4).static), (55, 0, False))
        self.assertEqual([d for _, cmd, d in s.requests() if cmd in (0x1D, 0x27)], [55, 3])
        self.assertTrue(s.sim.per_core_enabled)
        self.assertEqual(s.sim.misc_global_config, 0x8)
        # guard read-back verified, every write
        self.assertEqual([r.verified for r in report.steps], [None] * 5 + [True] * 4)
        self.assertTrue(all(r.status is StepStatus.OK and r.rc == 0 and r.rc_name == "Pass" for r in report.steps))
        write = report.steps[6]
        self.assertEqual(write.step.kind, StepKind.WRITE_VF)
        self.assertEqual(write.before, P4_BASELINE)
        self.assertEqual(write.after, s.sim.vf(4))
        self.assertAlmostEqual(write.applied_offset_mv, -31 * 1000 / 1024)
        self.assertAlmostEqual(write.applied_v_mv, 0.0)  # G-120: no OC point, the word carries no V target
        # nothing else moved
        fresh = SimMsrPort()
        for cpu in range(24):
            if cpu != 4:
                self.assertEqual(s.sim.vf(cpu), fresh.vf(cpu))
                self.assertEqual(s.sim.favored_word(cpu), fresh.favored_word(cpu))

    def test_an_offset_only_profile_sends_the_0x11_alone(self) -> None:
        """G-122: no OC ratio in the profile = no 0x1D (favored ratio) and no 0x27 (granular): the 0x11 word's ratio field is the current one, not a ratio the user set."""
        s = self.session()
        report = s.apply(profile(p4(offset_mv=-30)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual([r.step.kind for r in report.steps], [*P4_KINDS, StepKind.WRITE_PERCORE_ENABLE, StepKind.WRITE_VF])
        self.assertEqual([cmd for _, cmd, _ in s.write_requests()], [0x15, 0x11])  # the PerCore enable, then the V/F word
        self.assertEqual(s.sim.vf(4), P4_KEPT)
        self.assertEqual(s.sim.favored_word(4), SimMsrPort().favored_word(4))  # the favored word was not touched

    def test_audit_records_every_operation_and_the_operator(self) -> None:
        s = self.session()
        s.apply(profile(p4(offset_mv=-30, ratio=55)))
        records = s.audit()
        events = [r["event"] for r in records]
        self.assertEqual(events.count("attempt"), 9)
        self.assertEqual(events.count("result"), 9)
        self.assertEqual(events.count("readback"), 4)
        self.assertNotIn("refused", events)
        self.assertNotIn("trip", events)
        self.assertEqual({r["operator"] for r in records}, {OPERATOR})
        attempts = [r for r in records if r["event"] == "attempt"]
        self.assertEqual(
            [(a["cmd"], a["cpu"]) for a in attempts],
            [("0x14", 4), ("0x10", 4), ("0x1C", 4), ("0x09", 4), ("0x26", 4), ("0x15", 4), ("0x11", 4), ("0x1D", 4), ("0x27", 4)],
        )
        self.assertTrue(all(a["purpose"].startswith("apply:") for a in attempts))
        self.assertEqual(attempts[6]["baseline"], f"0x{P4_BASELINE.to_u32():08X}")
        self.assertEqual(attempts[6]["req_data"], f"0x{P4_KEPT.to_u32():08X}")
        self.assertEqual(attempts[7]["req_data"], "0x00000037")  # 0x1D = the ratio 55
        self.assertEqual(attempts[7]["baseline"], "0x00003737")  # the 0x1C word read
        self.assertEqual(attempts[8]["req_data"], "0x00000003")  # 0x27 = the 0x26 word
        self.assertFalse(s.lock_path.exists())  # the guard removed its lock file

    def test_every_transport_call_is_preceded_by_a_guard_record(self) -> None:
        s = self.session()
        s.apply(profile(p4(offset_mv=-30, ratio=55)))
        events = [r["event"] for r in s.audit()]
        self.assertEqual(s.transport.transacts, events.count("attempt") + events.count("readback"))
        self.assertEqual(s.transport.transacts, len(s.sim.requests))

    def test_revert_plan_restores_the_baseline_and_runs_in_a_new_session(self) -> None:
        s = self.session()
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual(
            [(r.cpu, r.cmd, r.p1, r.p2, r.data, r.purpose) for r in report.revert_plan],
            [
                (4, 0x11, 0, 0, P4_BASELINE.with_(v_target_raw=0).to_u32(), "revert"),  # G-124: no OC point = target 0
                (4, 0x1D, 0, 0, 55, "revert"),  # the ratio of the revert 0x11 word (what the 0x1D check wants)
                (4, 0x27, 0, 0, 3, "revert"),
                (4, 0x15, 0, 0, 0, "revert"),
            ],
        )
        # execute the CPU's three revert requests in a NEW guard session (the old one never executes them)
        fake, sim = s.fake, s.sim
        self.assertEqual(sim.vf(4), P4_KEPT)
        g2 = WriteGuard(
            MailboxTransport(sim, sleep=fake.sleep, clock=fake.clock),
            policy_with(dwell=0.0),
            AuditLog(self.tmp / "audit2.jsonl", fsync=False),
            session_token="second",
            boot_id_fn=lambda: "test-boot",  # T-016: never read the real /proc in a test
            env={ENV_FLAG: "1"},
            lock_path=self.tmp / "second.lock",
            clock=fake.clock,
        )
        confirm = Confirmation("second", "tester")
        for cmd, p1 in ((0x10, 0), (0x1C, 0), (0x09, 0x11), (0x26, 0)):
            g2.execute(Request(4, cmd, p1, 0, 0, "baseline"), confirm)
        results = [g2.execute(req, confirm) for req in report.revert_plan[:3]]
        self.assertTrue(all(r.verified for r in results))
        self.assertEqual(sim.vf(4), P4_BASELINE.with_(v_target_raw=0))  # G-124: the revert word carries no V target at 55x

    def test_dwell_is_honoured_by_waiting(self) -> None:
        s = self.session(policy=policy_with(dwell=60.0))
        start = s.fake.now
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertTrue(report.ok, report.format_report())
        self.assertGreaterEqual(s.fake.now - start, 8 * 60.0)  # eight waits between nine operations
        self.assertEqual(len([x for x in s.fake.sleeps if x >= 60.0]), 8)
        self.assertLess(s.fake.now - start, 9 * 60.0 + 5)  # and no wait after the last one

    def test_no_dwell_no_wait(self) -> None:
        s = self.session(policy=policy_with(dwell=0.0))
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertTrue(report.ok)
        self.assertTrue(all(x < 1.0 for x in s.fake.sleeps))  # only the transport's busy polls

    def test_a_sleep_that_does_not_advance_time_makes_the_guard_refuse_dwell(self) -> None:
        s = self.session(policy=policy_with(dwell=60.0))
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)), sleep=lambda seconds: None)
        self.assertEqual(report.stop_kind, StopKind.REFUSED)
        self.assertIn("DWELL", report.stopped_reason)
        self.assertEqual(len(report.steps), 2)  # step 2 (the second operation) was refused
        self.assertEqual(report.steps[-1].status, StepStatus.REFUSED)

    def test_progress_callback_sees_every_step(self) -> None:
        s = self.session()
        lines: list[str] = []
        s.apply(profile(p4(offset_mv=-30, ratio=55)), progress=lines.append)
        self.assertEqual(len([ln for ln in lines if ln.startswith("step ")]), 9)
        self.assertEqual(len([ln for ln in lines if ln.startswith("dwell")]), 8)

    def test_e_cluster_profile_writes_all_four_cpus(self) -> None:
        for wide in (False, True):
            with self.subTest(e_cluster_wide=wide):
                tmp = self.tmp / f"wide{wide}"
                tmp.mkdir()
                s = make_session(tmp, config=sim_config(e_cluster_wide=wide))
                report = s.apply(profile(e_core(12, offset_mv=-20, ratio=48)))
                self.assertTrue(report.ok, report.format_report())
                self.assertEqual(report.operations, 1 + 4 * 4 + 1 + 4 * 3)
                writes = [(c, d) for c, cmd, d in s.requests() if cmd == 0x11]
                self.assertEqual([c for c, _ in writes], [20, 21, 22, 23])  # XTU writes every E-core
                self.assertEqual({c for c, cmd, _ in s.requests() if cmd in (0x1D, 0x27)}, {20, 21, 22, 23})
                want = E_BASELINE.with_(offset_raw=-20)
                for cpu in (20, 21, 22, 23):
                    self.assertEqual(s.sim.vf(cpu), want)
                for cpu in (8, 11, 12, 19):  # the other clusters are untouched
                    self.assertEqual(s.sim.vf(cpu).offset_raw, 0)
                self.assertTrue(all(r.verified for r in report.steps if r.step.kind.is_write))
                self.assertTrue(any("cluster" in w for w in report.warnings))

    def test_mixed_p_and_e_profile(self) -> None:
        s = self.session()
        report = s.apply(profile(p4(offset_mv=-30, ratio=55), e_core(0, offset_mv=-20, ratio=48)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(report.operations, 1 + 5 * 4 + 1 + 5 * 3)
        self.assertEqual(s.sim.vf(4).offset_raw, -31)
        for cpu in (8, 9, 10, 11):
            self.assertEqual(s.sim.vf(cpu).offset_raw, -20)
        self.assertEqual(s.sim.vf(12).offset_raw, 0)
        # per CPU in the order to send them, the newest-written CPU first (0x11 before its 0x1D and 0x27), the 0x15 last
        self.assertEqual([r.cmd for r in report.revert_plan], [0x11, 0x1D, 0x27] * 5 + [0x15])
        self.assertEqual([r.cpu for r in report.revert_plan if r.cmd == 0x11], [11, 10, 9, 8, 4])

    def test_ratio_voltage_and_offset_together_report_the_applied_snapped_values(self) -> None:
        s = self.session()
        report = s.apply(profile(p4(offset_mv=-30, ratio=50, oc_voltage_mv=1300)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(s.sim.vf(4), VfWord(50, 0, False, -31))  # G-120: ratio 50 <= 57 = no OC point, the 1300 mV target is not written
        self.assertEqual(s.sim.favored_word(4) & 0xFF, 50)  # 0x1D carried the ratio of the 0x11 word
        writes = [r for r in report.steps if r.step.kind is StepKind.WRITE_VF]
        self.assertEqual(len(writes), 1)  # no V target to order against the falling ratio: one word
        write = writes[-1]
        self.assertAlmostEqual(write.applied_offset_mv, -30.2734375)
        self.assertIn(XTU_SHAPE_CAVEAT, report.warnings)
        text = report.format_report()
        self.assertIn(XTU_SHAPE_CAVEAT, text)
        self.assertIn("-30.27 mV", text)
        self.assertIn("raw -31", text)

    def test_the_report_shows_runtime_and_effective_offsets(self) -> None:
        s = self.session()
        text = s.apply(profile(p4(offset_mv=-30, ratio=55), e_core(0, offset_mv=-20, ratio=48))).format_report()
        self.assertIn("runtime offset -30.27 mV, effective -80.27 mV (assumed BIOS offset -50 mV)", text)  # P core: BIOS -50
        self.assertIn("runtime offset -19.53 mV, effective -19.53 mV (assumed BIOS offset +0 mV)", text)  # E core: BIOS 0

    def test_per_core_enable_already_set_skips_0x15(self) -> None:
        s = self.session(config=sim_config(misc_global_config=0x8))
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(report.operations, 8)
        self.assertEqual([cmd for _, cmd, _ in s.requests()], [0x14, 0x10, 0x1C, 0x09, 0x26, 0x11, 0x10, 0x1D, 0x1C, 0x27, 0x26])  # no 0x15
        self.assertEqual(s.sim.misc_global_config, 0x8)
        self.assertEqual([r.cmd for r in report.revert_plan], [0x11, 0x1D, 0x27])

    def test_per_core_enable_clear_writes_old_or_8(self) -> None:
        s = self.session(config=sim_config(misc_global_config=0x1))
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertTrue(report.ok, report.format_report())
        self.assertIn((4, 0x15, 0x9), s.requests())  # only bit 3 is added
        self.assertEqual(s.sim.misc_global_config, 0x9)
        self.assertEqual(report.revert_plan[-1].data, 0x1)


class PreflightBeforeAnything(Case):
    def test_needs_higher_limit_with_the_default_policy_sends_nothing(self) -> None:
        s = self.session(policy=default_m1_policy())
        with self.assertRaises(NeedsHigherLimit) as ctx:
            s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual((ctx.exception.required, ctx.exception.allowed), (13, 1))  # 9 operations + 4 read-backs
        self.assertEqual(s.sim.writes, [])  # no mailbox request - not even a read - was sent
        self.assertEqual(s.sim.reads, [])
        self.assertEqual(s.audit(), [])
        self.assertEqual(s.sim.vf(4), P4_BASELINE)

    def test_the_limit_is_judged_on_the_worst_case_before_any_read(self) -> None:
        s = self.session(policy=policy_with(max_ops=12), config=sim_config(misc_global_config=0x8))  # would need only 8 + 3 read-backs = 11
        with self.assertRaises(NeedsHigherLimit) as ctx:
            s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual((ctx.exception.required, ctx.exception.allowed), (13, 12))  # worst case: the 0x15 write and its read-back too
        self.assertEqual(s.sim.writes, [])

    def test_a_policy_that_forbids_the_reads_sends_nothing(self) -> None:
        policy = dataclasses.replace(policy_with(), cpus=frozenset(range(4)))
        s = self.session(policy=policy)
        with self.assertRaises(PlanRefused) as ctx:
            s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertTrue(all(r.reason == "CPU_NOT_ALLOWED" for r in ctx.exception.refusals))
        self.assertEqual(s.sim.writes, [])

    def test_predicted_refusals_abort_after_the_reads_and_before_every_write(self) -> None:
        # the refusals that depend on the words read: the planner's prediction and the real guard agree on the very same write
        cases = {
            "overvolt": (profile(p4(oc_voltage_mv=1400)), sim_config(initial_vf={4: P4_BASELINE.with_(ratio=58)}), [0x14, 0x10, 0x1C, 0x09, 0x26, 0x28]),  # an OC-point voltage also reads 0x28; G-124: only an OC point (58 > 57) carries a V target
            "ratio above baseline": (profile(p4(ratio=57)), None, [0x14, 0x10, 0x1C, 0x09, 0x26]),
            "static-mode baseline": (profile(p4(offset_mv=-30, ratio=55)), sim_config(initial_vf={4: P4_BASELINE.with_(static=True)}), [0x14, 0x10, 0x1C, 0x09, 0x26]),
        }
        for name, (prof, config, reads) in cases.items():
            with self.subTest(name):
                tmp = self.tmp / "".join(c if c.isalnum() else "_" for c in name)
                tmp.mkdir()
                s = make_session(tmp, config=config)
                before = s.sim.vf(4)
                with self.assertRaises(PlanRefused) as ctx:
                    s.apply(prof)
                refusal = ctx.exception.refusals[0]
                self.assertEqual((refusal.reason, refusal.cmd), ("CHECK_FAILED", 0x11))
                # only the reads reached the simulator: no 0x15, no 0x11, no 0x1D, no 0x27
                self.assertEqual([cmd for _, cmd, _ in s.requests()], reads)
                self.assertFalse(s.sim.per_core_enabled)
                self.assertEqual(s.sim.vf(4), before)
                # the partial report is attached
                self.assertEqual([r.step.kind for r in ctx.exception.report.steps], [*P4_KINDS, *([StepKind.READ_VMAX] if 0x28 in reads else [])])
                self.assertEqual(ctx.exception.report.revert_plan, ())
                # ... and the real guard refuses exactly that write with CHECK_FAILED (same predicate, same session facts)
                bad = next(st for st in ctx.exception.plan.steps if st.kind is StepKind.WRITE_VF).request()
                with self.assertRaises(GuardRefused) as g:
                    s.guard.execute(bad, s.confirm)
                self.assertEqual(g.exception.reason, Refusal.CHECK_FAILED)

    def test_values_outside_the_policy_window_are_refused_before_the_first_read(self) -> None:
        # D-016: the explicit bounds (offset window, policy ceilings) are judged on the profile alone, so nothing is sent at all
        cases = {
            "offset -150 mV": profile(p4(offset_mv=-150)),
            "offset -101 mV": profile(p4(offset_mv=-101)),
            "offset -51 mV (P-core runtime floor, D-015)": profile(p4(offset_mv=-51)),
            "positive offset (the default ceiling is 0)": profile(p4(offset_mv=1)),
        }
        for name, prof in cases.items():
            with self.subTest(name):
                tmp = self.tmp / "".join(c if c.isalnum() else "_" for c in name)
                tmp.mkdir()
                s = make_session(tmp)
                with self.assertRaises(PlanRefused) as ctx:
                    s.apply(prof)
                refusal = ctx.exception.refusals[0]
                self.assertEqual((refusal.reason, refusal.cmd), ("CHECK_FAILED", 0x11))
                self.assertIn("vf_bounded", refusal.detail)
                self.assertEqual(s.sim.requests, [])  # not even a read
                self.assertEqual(s.audit(), [])
                self.assertIsNone(ctx.exception.report)

    def test_the_p_core_floor_boundary_runs_end_to_end(self) -> None:
        s = self.session()
        report = s.apply(profile(p4(offset_mv=-50)))  # raw -51: exactly the runtime floor of a P core
        self.assertTrue(report.ok, report.format_report())
        self.assertEqual(s.sim.vf(4).offset_raw, -51)


class Faults(Case):
    def test_oc_locked_stops_at_the_0x15_write(self) -> None:
        s = self.session(config=sim_config(oc_locked=True))
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual(report.stop_kind, StopKind.RC)
        self.assertFalse(report.ok)
        last = report.steps[-1]
        self.assertEqual((last.step.cmd, last.status, last.rc, last.rc_name), (0x15, StepStatus.RC_ERROR, 1, "OCLocked"))
        self.assertIn("OCLocked", report.stopped_reason)
        self.assertEqual(len(report.steps), 6)
        self.assertEqual([cmd for _, cmd, _ in s.requests()], [0x14, 0x10, 0x1C, 0x09, 0x26, 0x15])  # no 0x11 after the stop
        self.assertEqual(s.sim.vf(4), P4_BASELINE)
        self.assertFalse(report.tripped)  # a non-zero rc is returned, not a trip
        self.assertEqual(report.revert_plan, ())  # the refused write changed nothing

    def test_oc_locked_on_the_0x11_write(self) -> None:
        s = self.session(config=sim_config(oc_locked=True, misc_global_config=0x8))
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual(report.stop_kind, StopKind.RC)
        self.assertEqual((report.steps[-1].step.cmd, report.steps[-1].rc_name), (0x11, "OCLocked"))
        self.assertEqual(len(report.steps), 6)
        self.assertEqual(s.sim.vf(4), P4_BASELINE)

    def test_undervolt_protection_with_a_negative_offset(self) -> None:
        s = self.session(config=sim_config(uvp=True))
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual(report.stop_kind, StopKind.RC)
        last = report.steps[-1]
        self.assertEqual((last.step.cmd, last.rc, last.rc_name), (0x11, 0x13, "UndervoltProtection"))
        self.assertIn("UndervoltProtection", report.stopped_reason)
        self.assertEqual(s.sim.vf(4), P4_BASELINE)  # the 0x11 changed nothing
        self.assertTrue(s.sim.per_core_enabled)  # ... but the earlier 0x15 did, so it is in the revert plan
        self.assertEqual([r.cmd for r in report.revert_plan], [0x15])
        self.assertIn("revert", report.format_report().lower())

    def test_silent_ignore_fails_the_readback_and_trips_the_guard(self) -> None:
        s = self.session()
        s.sim.silent_ignore(0x11)
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual(report.stop_kind, StopKind.UNVERIFIED)
        self.assertTrue(report.tripped)
        last = report.steps[-1]
        self.assertEqual((last.step.cmd, last.status, last.verified, last.rc), (0x11, StepStatus.UNVERIFIED, False, 0))
        self.assertEqual(last.after, P4_BASELINE)  # what the read-back actually shows
        self.assertIn("read-back", report.stopped_reason)
        self.assertIn("trip", [r["event"] for r in s.audit()])
        self.assertEqual(s.sim.vf(4), P4_BASELINE)
        self.assertEqual([r.cmd for r in report.revert_plan], [0x11, 0x15])  # the 0x11 was sent: it stays revertible
        self.assertIn("NOT verified", report.format_report())
        self.assertEqual([cmd for _, cmd, _ in s.requests() if cmd in (0x1D, 0x27)], [])  # nothing after the failed read-back
        # the guard stays tripped: a further request is refused TRIPPED and nothing is sent
        n = len(s.sim.requests)
        with self.assertRaises(GuardRefused) as ctx:
            s.guard.execute(Request(4, 0x10, 0, 0, 0, "after trip"), s.confirm)
        self.assertEqual(ctx.exception.reason, Refusal.TRIPPED)
        self.assertEqual(len(s.sim.requests), n)

    def test_silent_ignore_of_the_0x1d_write_trips_when_the_ratio_changed(self) -> None:
        s = self.session()
        s.sim.silent_ignore(0x1D)
        report = s.apply(profile(p4(offset_mv=-30, ratio=50)))  # the 0x1D carries 50, the stored favored byte stays 55
        self.assertEqual(report.stop_kind, StopKind.UNVERIFIED)
        self.assertTrue(report.tripped)
        last = report.steps[-1]
        self.assertEqual((last.step.kind, last.status, last.verified), (StepKind.WRITE_FAVORED, StepStatus.UNVERIFIED, False))
        self.assertEqual(last.readback_data & 0xFF, 55)
        self.assertEqual([cmd for _, cmd, _ in s.requests() if cmd == 0x27], [])  # the 0x27 was never sent
        self.assertEqual([r.cmd for r in report.revert_plan][:2], [0x11, 0x1D])  # 0x11 and 0x1D were sent: both stay revertible

    def test_silent_ignore_stops_an_e_cluster_apply_at_the_first_write(self) -> None:
        s = self.session()
        s.sim.silent_ignore(0x11)
        report = s.apply(profile(e_core(12, offset_mv=-20, ratio=48)))
        self.assertEqual(report.stop_kind, StopKind.UNVERIFIED)
        self.assertEqual(len([1 for _, cmd, _ in s.requests() if cmd == 0x11]), 1)  # cpus 21..23 were never written

    def test_stuck_busy_after_the_write_trips_the_guard(self) -> None:
        s = self.session(wrap_port=lambda sim: HookPort(sim, {0x11: lambda cpu: sim.stuck_busy(cpu, before_write=False)}))
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual(report.stop_kind, StopKind.TRANSPORT)
        self.assertTrue(report.tripped)
        last = report.steps[-1]
        self.assertEqual((last.step.cmd, last.status), (0x11, StepStatus.ERROR))
        self.assertIn("MailboxTimeout", last.detail)
        self.assertIn("write_sent=True", last.detail)  # the request reached the mailbox: outcome unknown
        self.assertIn("write_sent=True", report.stopped_reason)
        self.assertEqual([r.cmd for r in report.revert_plan], [0x11, 0x15])  # outcome unknown => still revertible
        self.assertEqual(len(report.steps), 7)
        self.assertEqual([r["event"] for r in s.audit()].count("trip"), 1)

    def test_stuck_busy_before_the_first_request_sends_nothing(self) -> None:
        s = self.session()
        s.sim.stuck_busy(4, before_write=True)
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual(report.stop_kind, StopKind.TRANSPORT)
        self.assertIn("write_sent=False", report.stopped_reason)
        self.assertEqual(len(report.steps), 1)
        self.assertEqual(s.sim.writes, [])  # MailboxTimeout before the write: no WRMSR at all
        self.assertEqual(report.revert_plan, ())

    def test_forced_rc_stops_the_report(self) -> None:
        s = self.session(config=sim_config(misc_global_config=0x8))
        s.sim.force_rc(0x11, 3)
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual(report.stop_kind, StopKind.RC)
        self.assertEqual(report.steps[-1].rc_name, "RatioExceedsLimits")
        self.assertIn("RatioExceedsLimits", report.format_report())
        self.assertEqual(s.sim.vf(4), P4_BASELINE)

    def test_a_non_zero_rc_on_the_0x1d_or_0x27_write_stops_there(self) -> None:
        for cmd, kind in ((0x1D, StepKind.WRITE_FAVORED), (0x27, StepKind.WRITE_GRANULAR)):
            with self.subTest(cmd=hex(cmd)):
                tmp = self.tmp / f"rc{cmd:x}"
                tmp.mkdir()
                s = make_session(tmp, config=sim_config(misc_global_config=0x8))
                s.sim.force_rc(cmd, 0x10)  # InvalidParam
                report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
                self.assertEqual(report.stop_kind, StopKind.RC)
                self.assertEqual((report.steps[-1].step.kind, report.steps[-1].rc_name), (kind, "InvalidParam"))
                self.assertEqual(s.sim.vf(4).offset_raw, -31)  # the 0x11 had already been written
                self.assertEqual(report.revert_plan[0].cmd, 0x11)

    def test_a_non_zero_rc_on_a_read_stops_before_any_write(self) -> None:
        for cmd in (0x10, 0x1C, 0x09, 0x26):
            with self.subTest(cmd=hex(cmd)):
                tmp = self.tmp / f"read{cmd:x}"
                tmp.mkdir()
                s = make_session(tmp)
                s.sim.force_rc(cmd, 5)
                report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
                self.assertEqual(report.stop_kind, StopKind.RC)
                self.assertEqual(s.write_requests(), [])
                self.assertEqual(report.steps[-1].step.cmd, cmd)

    def test_msr_error_is_a_transport_stop(self) -> None:
        s = self.session()
        s.sim.eio(4, 0x150)
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual(report.stop_kind, StopKind.TRANSPORT)
        self.assertIn("MsrError", report.stopped_reason)
        self.assertTrue(report.tripped)


class GuardRefusals(Case):
    def test_env_flag_absent(self) -> None:
        s = self.session(env={})
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual(report.stop_kind, StopKind.REFUSED)
        self.assertEqual(report.steps[0].status, StepStatus.REFUSED)
        self.assertIn("NO_ENV_FLAG", report.stopped_reason)
        self.assertEqual(s.sim.writes, [])
        self.assertEqual(report.operations, 0)
        self.assertEqual([r["event"] for r in s.audit()], ["refused"])
        self.assertEqual(s.audit()[0]["reason"], "NO_ENV_FLAG")

    def test_wrong_confirmation(self) -> None:
        s = self.session(confirm_token="not-the-token")
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual(report.stop_kind, StopKind.REFUSED)
        self.assertIn("BAD_CONFIRMATION", report.stopped_reason)
        self.assertEqual(s.sim.writes, [])

    def test_health_gate_before_refuses(self) -> None:
        s = self.session(health_gate=lambda phase: (False, "taint changed") if phase == "before" else (True, "ok"))
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual(report.stop_kind, StopKind.REFUSED)
        self.assertIn("HEALTH_GATE", report.stopped_reason)
        self.assertEqual(s.sim.writes, [])

    def test_health_gate_after_trips_and_stops_the_run(self) -> None:
        calls: list[str] = []

        def gate(phase: str) -> tuple[bool, str]:
            calls.append(phase)
            return (phase != "after", "delta not clean")

        s = self.session(health_gate=gate)
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual(report.stop_kind, StopKind.TRIPPED)
        self.assertTrue(report.tripped)
        self.assertEqual(len(report.steps), 1)  # stopped right after the first step, which itself succeeded
        self.assertEqual(report.steps[0].status, StepStatus.OK)
        self.assertEqual([cmd for _, cmd, _ in s.requests()], [0x14])
        self.assertEqual(calls, ["before", "after"])
        self.assertIn("tripped", report.stopped_reason)

    def test_a_guard_that_is_already_tripped_refuses_everything(self) -> None:
        s = self.session()
        s.sim.silent_ignore(0x11)
        s.apply(profile(p4(offset_mv=-30, ratio=55)))
        n = len(s.sim.requests)
        again = s.apply(profile(p4(offset_mv=-30, ratio=55)))
        self.assertEqual(again.stop_kind, StopKind.TRIPPED)
        self.assertIn("TRIPPED", again.stopped_reason)
        self.assertEqual(len(s.sim.requests), n)

    def test_session_limit_in_the_guard_is_still_enforced(self) -> None:
        # the executor's policy copy says 60, the guard was built with 3: the guard is authoritative and refuses the 4th request
        guard_policy = policy_with(max_ops=3, dwell=0.0)
        s = self.session(policy=guard_policy)
        report = s.apply(profile(p4(offset_mv=-30, ratio=55)), policy=policy_with(max_ops=60, dwell=0.0))
        self.assertEqual(report.stop_kind, StopKind.REFUSED)
        self.assertIn("SESSION_LIMIT", report.stopped_reason)
        self.assertEqual([cmd for _, cmd, _ in s.requests()], [0x14, 0x10, 0x1C])  # 3 operations (reads: no read-back)


class Report(Case):
    def test_format_report_of_a_clean_run(self) -> None:
        s = self.session()
        text = s.apply(profile(p4(offset_mv=-30, ratio=55))).format_report()
        for needle in (
            "Result: OK", "0x14", "0x10", "0x1C", "0x09", "0x26", "0x15", "0x11", "0x1D", "0x27", "rc 0 (Pass)", "read-back equal",
            "Revert plan", "NOT executed", "0x00054437", "favored ratio 55, fused ratio 55", "fused point ratio 55",
            "write favored-core word of P-Core 4", "write granular-ratio word of P-Core 4",
        ):
            self.assertIn(needle, text)
        self.assertIn(XTU_SHAPE_CAVEAT, text)
        self.assertIn("a reboot restores the BIOS-programmed state; a baseline of 0x0 cannot be restored by a write", text)  # the revert note, always
        self.assertIn(REVERT_NOTE, text)
        self.assertNotIn("not modelled", text)  # the T-014 ratio caveat is gone
        self.assertTrue(text.isascii())

    def test_format_report_of_a_stopped_run(self) -> None:
        s = self.session(config=sim_config(oc_locked=True))
        text = s.apply(profile(p4(offset_mv=-30, ratio=55))).format_report()
        self.assertIn("STOPPED (rc)", text)
        self.assertIn("OCLocked", text)
        self.assertIn("Stopped:", text)


if __name__ == "__main__":
    unittest.main()
