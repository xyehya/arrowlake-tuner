"""T-026 ``vf-dump`` tests: ``arltuner.cli.main`` + ``arltuner.vfdump`` against ``SimMsrPort`` with temp files and injected I/O.

Rules under test: docs/tasks/T-026-vf-dump.md. **No test touches a device, a real cpufreq file or a real process**: ``--device`` is only used for refusals
(``arltuner.cli.DevCpuMsrPort`` is a mock that must not be called), ``--measure`` runs on fake :class:`MeasureIO` objects, and ``SysfsMeasureIO`` is
exercised on a temp directory with a fake ``popen`` / ``which``. ``ARLTUNER_ALLOW_MAILBOX`` is only ever set through the CLI's ``env`` injection.
"""
from __future__ import annotations

import json
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from arltuner import vfdump
from arltuner.guard import ENV_FLAG, read_all
from arltuner.guard import policy as guard_policy
from arltuner.mailbox import VfWord
from arltuner.msr import SimConfig, SimMsrPort
from arltuner.msr.port import MsrError
from arltuner.msr.sim import POINT_OFFSETS_ARL_S, POINT_RATIOS_P_NONFAVORED_SIM
from arltuner.topology import arl_s_285k

from .test_cli import POLICY_TOML, TOKEN, CliCase

#: A policy that can hold the whole dump: reads of P2 0..10 (the walk's cap for a P-core), session / boot caps above the 232 of ``--all-e``.
DUMP_POLICY = (
    POLICY_TOML.replace("max_operations_per_session = 60", "max_operations_per_session = 300").replace("max_operations_per_boot = 200", "max_operations_per_boot = 400")
    + """
[[allow]]
name = "vf_point_read"
cmd = 0x10
p1 = [0]
p2 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
kind = "read"
"""
)
PERF_POLICY = DUMP_POLICY + """
[[msr_allow]]
name = "perf_status_read"
msr = 0x198
kind = "read"
"""
P_RATIOS = [8, 20, 30, 48, 53, 55, 56, 57]
E_RATIOS = [8, 15, 23, 30, 40, 46]


def non_favored_tables() -> dict[tuple[int, int], int]:
    """G-114: ``SimConfig.measured()`` gives every P-core the same table (F1); the flagging tests opt in to the T-026 SIM-ONLY per-core table."""
    return {(c, n): r for c in (1, 2, 4, 5, 6, 7) for n, r in enumerate(POINT_RATIOS_P_NONFAVORED_SIM, start=1)}


def non_favored_tables() -> dict[tuple[int, int], int]:
    """G-114: ``SimConfig.measured()`` gives every P-core the same table (F1); the flagging tests opt in to the T-026 SIM-ONLY per-core table."""
    return {(c, n): r for c in (1, 2, 4, 5, 6, 7) for n, r in enumerate(POINT_RATIOS_P_NONFAVORED_SIM, start=1)}


def dump_sim(**overrides: object) -> SimMsrPort:
    overrides.setdefault("per_point", True)
    overrides.setdefault("point_offsets", dict(POINT_OFFSETS_ARL_S))  # G-117: measured() starts at stock (0); these tests want the old BIOS-like offsets
    if "point_ratios" not in overrides:  # a caller that defines its own point tables opts out of the SIM-ONLY non-favored P table
        overrides.setdefault("cpu_point_ratios", non_favored_tables())
    overrides.setdefault("cpu_point_ratios", non_favored_tables())
    overrides.setdefault("reject_zero_fields", True)
    overrides.setdefault("oc_adds_point", False)  # G-120: measured() models the OC point; these walk tests predate it
    return SimMsrPort(SimConfig.measured(**overrides))


class DumpCase(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.dump_policy = self.write("dump.toml", DUMP_POLICY)

    def dump(self, *extra: object, policy: Path | None = None, sim: SimMsrPort | None = None, **kwargs):
        args = ["vf-dump", "--sim", "--policy", policy or self.dump_policy, "--audit", self.audit, "--lock", self.lock, *extra]
        return self.run_cli(*args, stdin=self.confirmed(), sim=sim or dump_sim(), **kwargs)

    def mailbox_requests(self) -> list[tuple[int, int, int]]:
        """(cpu, cmd, p2) of every request the simulator latched."""
        return [(r.cpu, r.cmd, r.p2) for r in self.sim.requests]


class Walk(DumpCase):
    def test_default_set_reads_all_p_cores_and_one_cpu_per_e_cluster(self) -> None:
        code, out, err = self.dump()
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(len(self.sim.requests), 8 * 10 + 4 * 8)  # P: OC + 8 points + the rc 0x11 at P2 = 9; E: OC + 6 points + the rc 0x11 at P2 = 7
        self.assertEqual(len(self.sim.writes), 112)  # every mailbox read is exactly one WRMSR
        self.assertTrue(all(cmd == 0x10 for _, cmd, _ in self.mailbox_requests()))
        self.assertEqual([c for c, _, p in self.mailbox_requests() if p == 0], [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20])
        self.assertEqual(max(p for c, _, p in self.mailbox_requests() if c < 8), 9)  # the walk stops at the first rc 0x11, below the P cap of 10
        self.assertEqual(max(p for c, _, p in self.mailbox_requests() if c >= 8), 7)
        for needle in ("P-Core 0", "P-Core 7", "E-cluster 0 (CPUs 8-11)", "E-cluster 3 (CPUs 20-23)", "-49.8 mV (raw -51)", "-14.6 mV (raw -15)", "57x", "46x"):
            self.assertIn(needle, out)
        self.assertEqual([r["event"] for r in read_all(self.audit)].count("attempt"), 112)

    def test_each_p_core_reports_its_own_table_and_ratio_differences_are_flagged(self) -> None:
        code, out, err = self.dump(wall_clock=lambda: 1_700_000_000.0)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("DIFFERENCES", out)
        self.assertIn("P-cores point 4: ratio differs (48x: cpu 0, 3; 45x: cpu 1, 2, 4, 5, 6, 7)", out)
        self.assertIn("P-cores point 8: ratio differs (57x: cpu 0, 3; 55x: cpu 1, 2, 4, 5, 6, 7)", out)
        self.assertNotIn("OC word: ratio differs", out)
        self.assertNotIn("offset differs", out)  # the BIOS offsets are per point and equal on every P-core in the sim
        self.assertEqual([VfWord.from_u32(self.sim.vf(1, 0, p).to_u32()).ratio for p in range(1, 9)], [8, 20, 30, 45, 50, 52, 54, 55])
        self.assertEqual([VfWord.from_u32(self.sim.vf(3, 0, p).to_u32()).ratio for p in range(1, 9)], P_RATIOS)
        code, out, _ = self.dump("--cpus", "0,3", sim=dump_sim(), wall_clock=lambda: 1_700_100_000.0)  # the two favored cores agree
        self.assertEqual(code, 0)
        self.assertNotIn("DIFFERENCES", out)
        self.assertIn("No offset or ratio differences", out)
        code, out, _ = self.dump("--cpus", "0,1", sim=dump_sim(), wall_clock=lambda: 1_700_200_000.0)
        self.assertIn("P-cores point 5: ratio differs (53x: cpu 0; 50x: cpu 1)", out)

    def test_one_confirmation_for_the_whole_dump(self) -> None:
        code, out, _ = self.dump()
        self.assertEqual(code, 0)
        self.assertEqual(out.count("CONFIRM"), 1)
        self.assertIn("up to 124 guard-mediated mailbox READ requests", out)  # the budget: 8 x 11 + 4 x 9 (the walk's caps)
        self.assertIn("normally 112", out)
        code, _, err = self.run_cli("vf-dump", "--sim", "--policy", self.dump_policy, "--audit", self.audit, "--lock", self.lock, stdin="CONFIRM nope\n", sim=dump_sim())
        self.assertEqual(code, 2, err)
        self.assertEqual(len(self.sim.requests), 0)

    def test_p_table_words_are_the_measured_curve(self) -> None:
        code, out, _ = self.dump("--cpus", "0")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.sim.requests), 10)
        words = [VfWord.from_u32(self.sim.vf(0, 0, p).to_u32()) for p in range(1, 9)]
        self.assertEqual([w.ratio for w in words], P_RATIOS)
        self.assertEqual([w.offset_raw for w in words], [0, 0, 0, 0, -51, -15, 0, -51])
        self.assertTrue(all(w.v_target_raw == 0 for w in words))  # fused points carry no voltage
        self.assertIn("OC word 0x00000039: ratio 57x, V target 0 (tracks the top fused point), adaptive", out)

    def test_all_e_reads_every_e_cpu(self) -> None:
        code, out, err = self.dump("--all-e")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(len(self.sim.requests), 8 * 10 + 16 * 8)  # 208 actual, budget 232
        self.assertIn("up to 232", out)
        self.assertEqual(sorted({c for c, _, _ in self.mailbox_requests()}), list(range(24)))
        self.assertIn("[read CPU 8, 9, 10, 11]", out)
        self.assertIn("cpu 11", out)

    def test_cpus_overrides_the_default_set(self) -> None:
        code, out, err = self.dump("--cpus", "0,12-13")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.mailbox_requests(), [(0, 0x10, p) for p in range(10)] + [(c, 0x10, p) for c in (12, 13) for p in range(8)])
        self.assertIn("E-cluster 1 (CPUs 12-15)   [read CPU 12, 13]", out)

    def test_cpus_with_all_e_notes_that_all_e_is_ignored(self) -> None:
        code, _, err = self.dump("--cpus", "0", "--all-e")
        self.assertEqual(code, 0)
        self.assertIn("--all-e is ignored", err)
        self.assertEqual(len(self.sim.requests), 10)

    def test_bad_or_unknown_cpus(self) -> None:
        code, _, err = self.dump("--cpus", "x")
        self.assertEqual(code, 1, err)
        code, _, err = self.dump("--cpus", "99")
        self.assertEqual(code, 2, err)
        self.assertIn("unknown Linux CPU 99", err)
        self.assertEqual(self.sim.requests, [])

    def test_the_end_of_a_table_is_rc_0x11_and_not_an_error(self) -> None:
        sim = dump_sim(point_counts={"P": 5, "E": 4})  # the machine answers rc 0x11 earlier than the known count
        code, out, err = self.dump("--cpus", "0,8", sim=sim)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.mailbox_requests(), [(0, 0x10, p) for p in range(7)] + [(8, 0x10, p) for p in range(6)])  # P2 = 0..5 + the 0x11 at 6; E 0..4 + 0x11 at 5
        self.assertIn("table end: rc 0x11 at P2=6", out)
        self.assertIn("table end: rc 0x11 at P2=5", out)
        self.assertNotIn("STOPPED", out)

    def test_the_known_count_is_not_a_stop(self) -> None:
        _, out, _ = self.dump("--cpus", "0,8")
        self.assertIn("table end: rc 0x11 at P2=9", out)  # P: 8 points, the terminator is read
        self.assertIn("table end: rc 0x11 at P2=7", out)  # E: 6 points

    def test_an_oc_ratio_above_the_top_point_creates_one_extra_point_that_is_walked(self) -> None:
        sim = dump_sim(oc_adds_point=True, initial_vf={0: VfWord(58, 0x5B4, False, 0), 8: VfWord(48, 0x4A0, False, 0)})
        code, out, err = self.dump("--cpus", "0,8", sim=sim)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.mailbox_requests(), [(0, 0x10, p) for p in range(11)] + [(8, 0x10, p) for p in range(9)])  # P: 9 points, 0x11 at 10; E: 7 points, 0x11 at 8
        self.assertIn("table end: rc 0x11 at P2=10", out)
        self.assertIn("table end: rc 0x11 at P2=8", out)
        self.assertIn("note: cpu 0 has 9 points (known fused count 8)", out)
        self.assertIn("note: cpu 8 has 7 points (known fused count 6)", out)
        self.assertIn("  9        58x", out)
        self.assertIn("  7        48x", out)

    def test_no_extra_point_when_the_oc_ratio_is_not_above_the_top_point(self) -> None:
        sim = dump_sim(oc_adds_point=True, initial_vf={0: VfWord(57, 0x5B4, False, 0)})
        code, out, _ = self.dump("--cpus", "0", sim=sim)
        self.assertEqual(code, 0)
        self.assertEqual(len(sim.requests), 10)
        self.assertNotIn("note: cpu 0", out)

    def test_the_hard_cap_is_reported_not_an_error_and_never_exceeded(self) -> None:
        sim = dump_sim(point_counts=None)  # a machine that never answers rc 0x11
        code, out, err = self.dump("--cpus", "0,8", sim=sim)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.mailbox_requests(), [(0, 0x10, p) for p in range(11)] + [(8, 0x10, p) for p in range(9)])  # P2 <= 10 for P, <= 8 for E
        self.assertIn("table end: cap reached (P2=10), no rc 0x11 seen", out)
        self.assertIn("table end: cap reached (P2=8), no rc 0x11 seen", out)

    def test_dwell_is_honoured_between_every_read(self) -> None:
        times: list[float] = []
        clock = {"t": 0.0}

        def sleep(s: float) -> None:
            clock["t"] += s

        sim = dump_sim()
        real = sim.wrmsr

        def wrmsr(cpu: int, msr: int, value: int) -> None:
            times.append(clock["t"])
            real(cpu, msr, value)

        sim.wrmsr = wrmsr  # type: ignore[method-assign]
        code, out, err = self.dump("--cpus", "0", sim=sim, clock=lambda: clock["t"], sleep=sleep)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(len(times), 10)
        self.assertTrue(all(b - a >= 60.0 for a, b in zip(times, times[1:])), times)


class Budget(DumpCase):
    def test_session_cap_too_low_is_refused_with_the_needed_count(self) -> None:
        low = self.write("low.toml", DUMP_POLICY.replace("max_operations_per_session = 300", "max_operations_per_session = 123"))
        code, out, err = self.dump(policy=low)
        self.assertEqual(code, 2, out + err)
        self.assertIn("needs up to 124 mailbox reads", err)
        self.assertIn("allows 123 per session", err)
        self.nothing_sent()
        self.assertFalse(self.audit.exists() and self.audit.read_text(encoding="utf-8").strip())

    def test_boot_cap_too_low_is_refused(self) -> None:
        low = self.write("low.toml", DUMP_POLICY.replace("max_operations_per_boot = 400", "max_operations_per_boot = 50"))
        code, _, err = self.dump(policy=low)
        self.assertEqual(code, 2, err)
        self.assertIn("needs up to 124", err)
        self.assertIn("only 50", err)
        self.nothing_sent()

    def test_boot_budget_counts_what_the_audit_log_already_holds(self) -> None:
        tight = self.write("tight.toml", DUMP_POLICY.replace("max_operations_per_boot = 400", "max_operations_per_boot = 250"))
        for n, (cpus, want) in enumerate((("0", 0), (None, 0), (None, 0), (None, 2))):  # 10 used, then 112 (122), 112 (234), then only 16 are left
            t = 1_700_000_000.0 + n * 100_000
            extra = ("--cpus", cpus) if cpus else ()
            code, _, err = self.dump(*extra, policy=tight, sim=dump_sim(), wall_clock=lambda t=t: t)
            self.assertEqual(code, want, err)
        self.assertIn("only 16", err)

    def test_measure_needs_one_spare_operation_in_both_caps(self) -> None:
        exact = self.write("exact.toml", PERF_POLICY.replace("max_operations_per_session = 300", "max_operations_per_session = 11"))
        with mock.patch.object(guard_policy, "PLAIN_MSR_READABLE", guard_policy.PLAIN_MSR_READABLE | {0x198}):
            code, _, err = self.dump("--cpus", "0", "--measure", policy=exact, sim=perf_sim(FakeIO()), measure_io_factory=lambda: FakeIO())
        self.assertEqual(code, 2, err)
        self.assertIn("(+1 spare", err)
        self.nothing_sent()

    def test_the_default_policy_cannot_hold_a_dump_and_has_no_point_reads(self) -> None:
        code, _, err = self.run_cli("vf-dump", "--sim", "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed(), sim=dump_sim())
        self.assertEqual(code, 2, err)
        self.assertIn("no allow entry", err)  # P2 1 is not allowlisted by the shipped policy
        self.nothing_sent()

    def test_an_uncovered_point_is_refused_before_anything_is_sent(self) -> None:
        no_p10 = self.write("nop10.toml", DUMP_POLICY.replace("p2 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]", "p2 = [1, 2, 3, 4, 5, 6, 7, 8, 9]"))  # a P-core may be read up to P2 = 10
        code, _, err = self.dump(policy=no_p10)
        self.assertEqual(code, 2, err)
        self.assertIn("P2=0xa", err)
        self.nothing_sent()
        code, out, err = self.dump("--cpus", "8", policy=no_p10, sim=dump_sim())  # an E CPU only needs 0..8
        self.assertEqual((code, err), (0, ""), out)

    def test_a_cpu_outside_the_policy_is_refused_before_anything_is_sent(self) -> None:
        few = self.write("few.toml", DUMP_POLICY.replace('cpus = ["0-23"]', "cpus = [0, 1, 2, 3, 4, 5, 6, 7, 8]"))
        code, _, err = self.dump(policy=few)
        self.assertEqual(code, 2, err)
        self.assertIn("CPU_NOT_ALLOWED", err)
        self.nothing_sent()

    def test_the_guard_gets_exactly_the_budget_as_session_cap(self) -> None:
        from arltuner.guard import WriteGuard

        with mock.patch("arltuner.cli.WriteGuard", wraps=WriteGuard) as guard_cls:
            self.assertEqual(self.dump("--cpus", "0,8")[0], 0)
        self.assertEqual(guard_cls.call_args.args[1].max_operations_per_session, 20)  # 11 + 9, no measure: no spare

    def test_env_flag_absent(self) -> None:
        code, _, err = self.dump(env={})
        self.assertEqual(code, 2, err)
        self.assertIn(ENV_FLAG, err)
        self.nothing_sent()


class DeviceRefusals(CliCase):
    def test_device_refusals_happen_before_a_port_exists(self) -> None:
        with mock.patch("arltuner.cli.DevCpuMsrPort") as dev:
            policy = self.write("dump.toml", DUMP_POLICY)
            args = ["vf-dump", "--device", "--policy", policy, "--audit", self.audit, "--lock", self.lock]
            code, _, err = self.run_cli(*args, stdin=self.confirmed(), env={})
            self.assertEqual(code, 2, err)
            code, _, err = self.run_cli(*args, stdin="CONFIRM wrong\n")
            self.assertEqual(code, 2, err)
            code, _, err = self.run_cli(*args, "--measure", stdin=self.confirmed())  # no msr_allow for 0x198 in this policy
            self.assertEqual(code, 2, err)
            self.assertIn("0x198", err)
            dev.assert_not_called()
        self.nothing_sent()


class StopRules(DumpCase):
    def test_other_rc_at_the_first_word_stops_everything(self) -> None:
        sim = dump_sim()
        sim.force_rc(0x10, 0x05)
        code, out, err = self.dump(sim=sim)
        self.assertEqual(code, 3, out + err)
        self.assertIn("completion code 5", err)
        self.assertEqual(len(sim.requests), 1)
        self.assertIn("STOPPED", out)

    def test_rc_0x11_at_p2_0_is_not_an_end_of_table(self) -> None:
        sim = dump_sim()
        sim.force_rc(0x10, 0x11)
        code, _, err = self.dump(sim=sim)
        self.assertEqual(code, 3, err)
        self.assertEqual(len(sim.requests), 1)

    def test_other_rc_midway_prints_what_was_read_and_writes_partial_json(self) -> None:
        class Flaky(SimMsrPort):
            def _cmd_vf_read(self, cpu: int, p1: int, p2: int, data: int) -> tuple[int, int]:
                if (cpu, p2) == (2, 3):
                    return 0x05, 0
                return super()._cmd_vf_read(cpu, p1, p2, data)

        sim = Flaky(SimConfig.measured(per_point=True, reject_zero_fields=True))
        out_file = self.tmp / "partial.json"
        code, out, err = self.dump("--json", out_file, sim=sim)
        self.assertEqual(code, 3, out + err)
        self.assertEqual(len(sim.requests), 2 * 10 + 4)  # P0, P1 complete; P2: OC, 1, 2, then the failing P2=3
        self.assertIn("P-Core 0", out)
        self.assertIn("P-Core 1", out)
        self.assertIn("P-Core 2", out)
        self.assertNotIn("P-Core 3", out)
        self.assertIn("STOPPED: cpu 2 P2=3", out)
        data = json.loads(out_file.read_text(encoding="utf-8"))
        self.assertFalse(data["complete"])
        self.assertEqual([u["name"] for u in data["units"]], ["P-Core 0", "P-Core 1", "P-Core 2"])
        self.assertEqual(len(data["units"][2]["cpus"][0]["points"]), 2)
        self.assertEqual(data["units"][2]["cpus"][0]["end"], "stopped")

    def test_a_mailbox_timeout_stops_the_dump_with_exit_3(self) -> None:
        sim = dump_sim()
        sim.stuck_busy(1, before_write=True)
        code, out, err = self.dump(sim=sim)
        self.assertEqual(code, 3, out + err)
        self.assertIn("hardware error", err)
        self.assertEqual(len(sim.requests), 10)  # P0 complete, P1 never latched
        self.assertIn("P-Core 0", out)

    def test_a_tripped_guard_afterwards_is_exit_3_but_the_data_is_printed(self) -> None:
        calls = {"n": 0}

        def gate(phase: str) -> tuple[bool, str]:
            calls["n"] += 1
            return (False, "new taint bit") if phase == "after" and calls["n"] > 6 else (True, "ok")

        code, out, err = self.dump("--cpus", "0", health_gate=gate)
        self.assertEqual(code, 3, out + err)
        self.assertIn("P-Core 0", out)
        self.assertIn("STOPPED", out)


class Differences(DumpCase):
    def test_an_offset_that_differs_between_p_cores_is_flagged(self) -> None:
        sim = dump_sim(cpu_point_offsets={(3, 5): -40})
        code, out, err = self.dump(sim=sim)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("DIFFERENCES", out)
        self.assertIn("P-cores point 5: offset differs", out)
        self.assertIn("-39.1 mV (raw -40): cpu 3", out)
        self.assertIn("-49.8 mV (raw -51): cpu 0, 1, 2, 4, 5, 6, 7", out)

    def test_e_cpus_of_one_cluster_that_differ_are_flagged_with_all_e(self) -> None:
        sim = dump_sim(cpu_point_offsets={(10, 2): 5})
        code, out, err = self.dump("--all-e", sim=sim)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("E-cluster 0 point 2: offset differs", out)
        self.assertIn("+4.9 mV (raw 5): cpu 10", out)
        self.assertNotIn("E-cluster 1 point", out)

    def test_e_cpus_of_one_cluster_that_differ_in_ratio_are_flagged(self) -> None:
        sim = dump_sim(cpu_point_ratios={(9, 3): 24})
        code, out, _ = self.dump("--cpus", "8,9", sim=sim)
        self.assertEqual(code, 0)
        self.assertIn("E-cluster 0 point 3: ratio differs (23x: cpu 8; 24x: cpu 9)", out)


class Json(DumpCase):
    def test_json_has_the_same_data(self) -> None:
        out_file = self.tmp / "dump.json"
        code, out, err = self.dump("--json", out_file)
        self.assertEqual((code, err), (0, ""), out)
        data = json.loads(out_file.read_text(encoding="utf-8"))
        self.assertTrue(data["complete"])
        self.assertTrue(data["simulator"])
        self.assertIsNone(data["stopped"])
        self.assertEqual(len(data["units"]), 12)
        p1 = data["units"][1]["cpus"][0]
        self.assertEqual([p["ratio"] for p in p1["points"]], [8, 20, 30, 45, 50, 52, 54, 55])  # every P-core reports its own table
        p0 = data["units"][0]
        self.assertEqual(p0["name"], "P-Core 0")
        cpu0 = p0["cpus"][0]
        self.assertEqual([p["ratio"] for p in cpu0["points"]], P_RATIOS)
        self.assertEqual([p["offset_raw"] for p in cpu0["points"]], [0, 0, 0, 0, -51, -15, 0, -51])
        self.assertAlmostEqual(cpu0["points"][4]["offset_mv"], -49.805, places=2)
        self.assertEqual(cpu0["oc"]["ratio"], 57)
        self.assertEqual(cpu0["points"][0]["word"], "0x00000008")
        e3 = data["units"][-1]
        self.assertEqual(e3["name"], "E-cluster 3 (CPUs 20-23)")
        self.assertEqual(e3["cluster_cpus"], [20, 21, 22, 23])
        self.assertEqual([p["ratio"] for p in e3["cpus"][0]["points"]], E_RATIOS)
        self.assertEqual({(d["scope"], d["field"]) for d in data["differences"]}, {("P-cores", "ratio")})  # favored vs non-favored P-cores
        self.assertEqual([d["point"] for d in data["differences"]], [4, 5, 6, 7, 8])
        self.assertEqual(data["differences"][0]["values"], {"0": "48x", "1": "45x", "2": "45x", "3": "48x", "4": "45x", "5": "45x", "6": "45x", "7": "45x"})
        self.assertIsNone(p0["measured"])

    def test_json_in_a_missing_directory_is_a_usage_error_before_anything_is_sent(self) -> None:
        code, _, err = self.dump("--json", self.tmp / "nope" / "x.json")
        self.assertEqual(code, 1, err)
        self.nothing_sent()


# ----------------------------------------------------------------------------------------------------------
# --measure
# ----------------------------------------------------------------------------------------------------------
class FakeLoad:
    def __init__(self, io: FakeIO, cpus: tuple[int, ...]) -> None:
        self.io, self.cpus = io, cpus
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeIO:
    """Fake cpufreq + load generator. ``vid(kind, ratio)`` is what the "CPU" requests while it runs at the capped ratio."""

    def __init__(self, p_cap: int = 5_700_000, e_cap: int = 4_600_000) -> None:
        self.caps = {c: (p_cap if c < 8 else e_cap) for c in range(24)}
        self.original = dict(self.caps)
        self.writes: list[tuple[int, int]] = []
        self.loads: list[FakeLoad] = []
        self.problem: str | None = None
        self.fail_restore_cpu: int | None = None
        self.running_offset = 0  # added to the running ratio (a MISMATCH injector)
        self.wobble: list[int] = []  # per-sample offsets added to the running ratio, cycled (a wavering ratio)
        self.exact_ratios: set[int] = set()  # E cap ratios (cap // 100 MHz) at which the E core runs AT the cap, not one bin below
        self.max_khz = {c: (5_700_000 if c < 8 else 4_700_000) for c in range(24)}  # cpuinfo_max_freq
        self._wobble_i = 0
        self.no_temp = False  # no coretemp sensor
        self.temp_step = 0.0  # each temperature read is this much hotter than the last (a warming core)
        self._temp_extra = 0.0

    def check(self) -> str | None:
        return self.problem

    def read_cap(self, cpu: int) -> int:
        return self.caps[cpu]

    def read_max_khz(self, cpu: int) -> int:
        return self.max_khz[cpu]

    def write_cap(self, cpu: int, khz: int) -> None:
        if cpu == self.fail_restore_cpu and khz == self.original[cpu]:
            raise OSError("EBUSY")
        self.writes.append((cpu, khz))
        self.caps[cpu] = khz

    def start_load(self, cpus: Sequence[int], seconds: float) -> FakeLoad:
        load = FakeLoad(self, tuple(cpus))
        self.loads.append(load)
        return load

    def read_temp_c(self, cpu: int) -> float | None:
        if self.no_temp:
            return None
        self._temp_extra += self.temp_step
        return 40.0 + self.running_ratio(cpu) * 0.4 + self._temp_extra

    def running_ratio(self, cpu: int) -> int:
        cap = self.caps[cpu]
        quirk = 1 if cpu >= 8 and cap // 100_000 not in self.exact_ratios else 0
        return cap // 100_000 - quirk + self.running_offset  # the intel_pstate quirk: E runs one bin below the cap (except at ``exact_ratios``)

    def word(self, cpu: int) -> int:
        ratio = self.running_ratio(cpu)
        if self.wobble:
            ratio += self.wobble[self._wobble_i % len(self.wobble)]
            self._wobble_i += 1
        vid = 4096 + ratio * 128 + (100 if cpu >= 8 else 0)  # raw 1/8192 V: exact, no float rounding
        return (vid << 32) | (ratio << 8)


class PerfSim(SimMsrPort):
    """The simulator with MSR 0x198 answered from a :class:`FakeIO` (the sim does not model it)."""

    io: FakeIO

    def rdmsr(self, cpu: int, msr: int) -> int:
        if msr == 0x198:
            return self.io.word(cpu)
        return super().rdmsr(cpu, msr)


def perf_sim(io: FakeIO, **overrides: object) -> PerfSim:
    overrides.setdefault("per_point", True)
    overrides.setdefault("oc_adds_point", False)  # G-120: E starts at 48x > 46 here; these measure tests predate the modelled OC point
    overrides.setdefault("initial_vf", {0:VfWord.from_u32(0x39), 8: VfWord(48, 0x4A0, False, 0)})
    sim = PerfSim(SimConfig.measured(**overrides))
    sim.io = io
    return sim


class Measure(DumpCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(guard_policy, "PLAIN_MSR_READABLE", guard_policy.PLAIN_MSR_READABLE | {0x198})  # policy.py is not T-026's: see the report
        patcher.start()
        self.addCleanup(patcher.stop)
        self.perf_policy = self.write("perf.toml", PERF_POLICY)
        self.io = FakeIO()

    def measure(self, *extra: object, io: FakeIO | None = None, sim: SimMsrPort | None = None, **kwargs):
        io = io or self.io
        return self.dump("--measure", *extra, policy=self.perf_policy, sim=sim or perf_sim(io), measure_io_factory=lambda: io, **kwargs)

    def test_measure_is_refused_in_the_simulator_without_injected_io(self) -> None:
        code, _, err = self.dump("--measure", policy=self.perf_policy)
        self.assertEqual(code, 2, err)
        self.assertIn("--measure needs --device", err)
        self.nothing_sent()

    def test_measure_is_refused_when_the_policy_does_not_allow_0x198(self) -> None:
        code, _, err = self.dump("--measure", sim=perf_sim(self.io), measure_io_factory=lambda: self.io)  # DUMP_POLICY: no msr_allow
        self.assertEqual(code, 2, err)
        self.assertIn("0x198", err)
        self.nothing_sent()
        self.assertEqual(self.io.writes, [])

    def test_measure_is_refused_when_stress_ng_is_missing(self) -> None:
        self.io.problem = "stress-ng was not found on PATH"
        code, _, err = self.measure()
        self.assertEqual(code, 2, err)
        self.assertIn("stress-ng", err)
        self.nothing_sent()

    def test_p_core_and_e_cluster_caps_loads_vids_and_restore(self) -> None:
        sim = perf_sim(self.io)
        out_file = self.tmp / "m.json"
        code, out, err = self.measure("--cpus", "0,8", "--json", out_file, sim=sim)
        self.assertEqual((code, err), (0, ""), out)
        p_writes = [khz for cpu, khz in self.io.writes if cpu == 0]
        self.assertEqual(p_writes[:-1], [r * 100_000 for r in P_RATIOS])  # P cap = ratio x 100 MHz
        self.assertEqual(p_writes[-1], 5_700_000)  # restored
        for cpu in (8, 9, 10, 11):  # the whole cluster is capped: ratio x 100 MHz first (runs one bin low in the fake), then (ratio + 1) x 100 MHz; 48x is above 4600 MHz: skipped
            self.assertEqual([khz for c, khz in self.io.writes if c == cpu][:-1], [k for r in E_RATIOS for k in (r * 100_000, (r + 1) * 100_000)])
        self.assertEqual(self.io.caps, self.io.original)  # everything restored
        self.assertEqual([load.cpus for load in self.io.loads], [(0,)] * 8 + [(8, 9, 10, 11)] * 12)  # E: all four CPUs of the cluster loaded
        self.assertTrue(all(load.stopped for load in self.io.loads))
        self.assertIn("measured VID @ core temp", out)
        self.assertIn("1.3906 V @ 63 \u00b0C", out)  # P 57x: (4096 + 57 x 128) / 8192, core at 40 + 57 x 0.4 = 62.8 C
        self.assertIn("1.2310 V @ 58 \u00b0C", out)  # E 46x: (4096 + 46 x 128 + 100) / 8192, E runs at 46: 40 + 18.4 C
        self.assertIn("note 48x: 48x (4800 MHz) is above the current scaling_max_freq (4600 MHz)", out)
        data = json.loads(out_file.read_text(encoding="utf-8"))
        e = data["units"][1]["measured"]
        self.assertEqual([m["ratio"] for m in e], E_RATIOS + [48])
        self.assertEqual(e[-1]["status"], "skipped")
        self.assertEqual(e[-2]["status"], "ok")
        self.assertAlmostEqual(e[-2]["vid_min_v"], 10084 / 8192, places=6)
        self.assertAlmostEqual(e[-2]["temp_min_c"], 58.4, places=6)
        self.assertAlmostEqual(data["units"][0]["measured"][-1]["temp_max_c"], 62.8, places=6)
        events = [r["event"] for r in read_all(self.audit)]
        self.assertEqual(events.count("attempt"), 10 + 8)  # only the mailbox reads count
        self.assertEqual(events.count("msr_read"), (8 + 12) * 30)  # 30 audited 0x198 samples per cap attempt: P once, E twice (the first cap runs one bin low)

    def test_the_temperature_range_is_shown_when_the_core_warms_up_while_sampling(self) -> None:
        self.io.temp_step = 1.0
        out_file = self.tmp / "m.json"
        code, out, err = self.measure("--cpus", "0", "--json", out_file)
        self.assertEqual((code, err), (0, ""), out)
        first = json.loads(out_file.read_text(encoding="utf-8"))["units"][0]["measured"][0]  # ratio 8, 30 samples: +1..+30 C
        self.assertAlmostEqual(first["temp_max_c"] - first["temp_min_c"], 29.0, places=6)
        self.assertRegex(out, r"0\.6250 V @ \d+\.\.\d+ °C")

    def test_no_temperature_sensor_is_n_a_and_never_stops_the_measurement(self) -> None:
        self.io.no_temp = True
        out_file = self.tmp / "m.json"
        code, out, err = self.measure("--cpus", "0", "--json", out_file)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("1.3906 V @ n/a", out)
        m = json.loads(out_file.read_text(encoding="utf-8"))["units"][0]["measured"][-1]
        self.assertEqual((m["status"], m["temp_min_c"], m["temp_max_c"]), ("ok", None, None))

    def test_a_failing_temperature_read_is_n_a_not_an_error(self) -> None:
        def boom(cpu: int) -> float | None:
            raise OSError("EIO")

        self.io.read_temp_c = boom  # type: ignore[method-assign]
        code, out, err = self.measure("--cpus", "0")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("@ n/a", out)

    def test_a_running_ratio_that_is_not_the_target_is_a_mismatch_without_voltage(self) -> None:
        self.io.running_offset = 1
        out_file = self.tmp / "m.json"
        code, out, err = self.measure("--cpus", "0", "--json", out_file)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("MISMATCH (", out)
        self.assertNotIn(" V\n", out.split("table end")[0].split("measured VID")[1])
        data = json.loads(out_file.read_text(encoding="utf-8"))
        for m in data["units"][0]["measured"]:
            self.assertEqual(m["status"], "MISMATCH")
            self.assertIsNone(m["vid_min_v"])
        self.assertEqual(self.io.caps, self.io.original)

    def test_caps_are_restored_when_a_read_fails_midway(self) -> None:
        sim = perf_sim(self.io)
        calls = {"n": 0}
        real = sim.rdmsr

        def rdmsr(cpu: int, msr: int) -> int:
            if msr == 0x198:
                calls["n"] += 1
                if calls["n"] == 12:
                    raise MsrError(5, "boom")
            return real(cpu, msr)

        sim.rdmsr = rdmsr  # type: ignore[method-assign]
        code, out, err = self.measure("--cpus", "0", sim=sim)
        self.assertEqual(code, 3, out + err)
        self.assertEqual(self.io.caps, self.io.original)
        self.assertTrue(all(load.stopped for load in self.io.loads))
        self.assertIn("measurement error", err)
        self.assertIn("P-Core 0", out)  # the table that was read is still printed

    def test_a_failed_restore_is_reported_loudly(self) -> None:
        self.io.fail_restore_cpu = 0
        code, out, err = self.measure("--cpus", "0")
        self.assertEqual(code, 3, out + err)
        self.assertIn("could NOT restore scaling_max_freq of cpu 0", err)

    def test_the_guard_still_gates_the_plain_reads(self) -> None:
        sim = perf_sim(self.io)
        code, out, err = self.measure("--cpus", "0", sim=sim, health_gate=lambda phase: (True, "ok"))
        self.assertEqual(code, 0, out + err)


class MeasureGroupUnit(unittest.TestCase):
    """``measure_group`` without the guard: cap math, skipping, restore on any exception (also KeyboardInterrupt)."""

    def group(self, kind: str, cpus: tuple[int, ...], ratios: tuple[int, ...]) -> vfdump.MeasureGroup:
        return vfdump.MeasureGroup("g", kind, cpus, cpus[0], ratios)

    def test_cap_math(self) -> None:
        self.assertEqual(vfdump.cap_khz("P", 57), 5_700_000)
        self.assertEqual(vfdump.cap_khz("E", 46), 4_700_000)

    def test_restore_on_keyboard_interrupt(self) -> None:
        io = FakeIO()

        def read_perf(cpu: int) -> int:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            vfdump.measure_group(io, self.group("E", (8, 9, 10, 11), (40, 46)), read_perf, lambda s: None, [])
        self.assertEqual(io.caps, io.original)
        self.assertTrue(io.loads[0].stopped)

    def test_the_load_is_stopped_even_if_sleep_raises(self) -> None:
        io = FakeIO()

        def sleep(s: float) -> None:
            raise RuntimeError("x")

        with self.assertRaises(RuntimeError):
            vfdump.measure_group(io, self.group("P", (0,), (30,)), io.word, sleep, [])
        self.assertTrue(io.loads[0].stopped)
        self.assertEqual(io.caps, io.original)

    def test_ratios_above_the_current_max_are_skipped_not_measured(self) -> None:
        io = FakeIO(p_cap=5_000_000)
        out = vfdump.measure_group(io, self.group("P", (0,), (50, 53)), io.word, lambda s: None, [])
        self.assertEqual([(m.ratio, m.status) for m in out], [(50, "ok"), (53, "skipped")])
        self.assertEqual(len(io.loads), 1)

    def test_a_restore_error_does_not_mask_the_first_error(self) -> None:
        io = FakeIO()
        io.fail_restore_cpu = 0
        problems: list[str] = []

        def read_perf(cpu: int) -> int:
            raise ValueError("first")

        with self.assertRaises(ValueError):
            vfdump.measure_group(io, self.group("P", (0,), (30,)), read_perf, lambda s: None, problems)
        self.assertEqual(len(problems), 1)

    def measure_one(self, io: FakeIO, kind: str, ratio: int, **kw: int) -> tuple[vfdump.RatioMeasurement, list[int]]:
        cpus = (0,) if kind == "P" else (8, 9, 10, 11)
        out = vfdump.measure_group(io, self.group(kind, cpus, (ratio,)), io.word, lambda s: None, [], **kw)
        self.assertEqual(io.caps, io.original)
        return out[0], [khz for cpu, khz in io.writes if cpu == cpus[0]][:-1]

    def test_the_exact_cap_is_tried_first_and_kept_when_it_works(self) -> None:
        m, tried = self.measure_one(FakeIO(), "P", 45)
        self.assertEqual((m.status, m.cap_khz, tried, m.samples_kept, m.samples_total), ("ok", 4_500_000, [4_500_000], 30, 30))
        io = FakeIO()
        io.exact_ratios = {30}
        m, tried = self.measure_one(io, "E", 30)  # E at 30x: cap 3000 MHz runs 30x
        self.assertEqual((m.status, m.cap_khz, tried), ("ok", 3_000_000, [3_000_000]))

    def test_a_ratio_that_settles_below_is_retried_with_one_bin_more(self) -> None:
        io = FakeIO()
        m, tried = self.measure_one(io, "E", 40)  # cap 4000 MHz runs 39x in the fake: retry 4100 MHz
        self.assertEqual((m.status, m.cap_khz, tried), ("ok", 4_100_000, [4_000_000, 4_100_000]))
        self.assertIn("ratio + 1 bin", m.note)
        self.assertEqual(len(io.loads), 2)
        self.assertTrue(all(load.stopped for load in io.loads))

    def test_a_ratio_that_runs_above_is_a_mismatch_without_a_retry(self) -> None:
        io = FakeIO()
        io.running_offset = 1
        m, tried = self.measure_one(io, "P", 45)
        self.assertEqual((m.status, tried, m.vid_min, m.samples_kept), ("MISMATCH", [4_500_000], None, 0))
        self.assertIn("above", m.note)

    def test_still_below_after_the_retry_is_a_mismatch(self) -> None:
        io = FakeIO()
        io.running_offset = -2
        m, tried = self.measure_one(io, "P", 45)
        self.assertEqual((m.status, tried), ("MISMATCH", [4_500_000, 4_600_000]))
        self.assertIn("below", m.note)

    def test_the_cap_never_exceeds_cpuinfo_max_freq(self) -> None:
        io = FakeIO()
        io.max_khz[0] = 4_500_000
        io.running_offset = -1
        m, tried = self.measure_one(io, "P", 45)
        self.assertEqual((m.status, tried), ("MISMATCH", [4_500_000]))  # 4600 MHz is not offered
        self.assertEqual(vfdump.caps_to_try(45, 4_500_000), [4_500_000])
        self.assertEqual(vfdump.caps_to_try(45), [4_500_000, 4_600_000])

    def test_a_wavering_ratio_keeps_only_the_samples_at_the_target(self) -> None:
        io = FakeIO()
        io.wobble = [0, 0, 0, -3, 0, 3]  # 4 of 6 on target: 20 of 30
        m, tried = self.measure_one(io, "P", 45)
        self.assertEqual((m.status, m.samples_kept, m.samples_total, tried), ("ok", 20, 30, [4_500_000]))
        self.assertEqual(m.seen_ratios, (42, 45, 48))
        self.assertAlmostEqual(m.vid_min or 0, (4096 + 45 * 128) / 8192, places=6)  # off-target samples' VIDs are not in the range
        self.assertAlmostEqual(m.vid_max or 0, (4096 + 45 * 128) / 8192, places=6)
        self.assertIn("[20/30 at target]", vfdump._vid_text(m))

    def test_too_few_matching_samples_is_a_mismatch(self) -> None:
        io = FakeIO()
        io.wobble = [3] * 5 + [0] * 5  # all P samples above/at target: 15 on target
        m, _ = self.measure_one(io, "P", 45, samples=10, min_valid=6)  # only 5 at target
        self.assertEqual((m.status, m.samples_kept, m.samples_total), ("MISMATCH", 5, 10))
        m, _ = self.measure_one(FakeIO(), "P", 45, samples=10, min_valid=11)
        self.assertEqual(m.status, "MISMATCH")
        m, _ = self.measure_one(FakeIO(), "P", 45, samples=10, min_valid=10)
        self.assertEqual(m.status, "ok")

    def test_groups_add_the_oc_ratio_only_when_it_is_above_the_top_point(self) -> None:
        topo = arl_s_285k()
        e = vfdump.CpuCurve(8, "E", 0, "E-Core 0", oc=VfWord(48, 0, False, 0).to_u32(), points=[VfWord(r, 0, False, 0).to_u32() for r in E_RATIOS])
        p = vfdump.CpuCurve(0, "P", None, "P-Core 0", oc=VfWord(57, 0, False, 0).to_u32(), points=[VfWord(r, 0, False, 0).to_u32() for r in P_RATIOS])
        groups = vfdump.measure_groups(vfdump.units_of([p, e], topo))
        self.assertEqual(groups[0].ratios, tuple(P_RATIOS))
        self.assertEqual(groups[1].ratios, tuple(E_RATIOS + [48]))
        self.assertEqual((groups[1].cpus, groups[1].rep), ((8, 9, 10, 11), 8))


class SysfsIO(unittest.TestCase):
    def test_reads_and_writes_the_scaling_max_freq_files_under_the_given_root(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cpu3" / "cpufreq").mkdir(parents=True)
            (root / "cpu3" / "cpufreq" / "scaling_max_freq").write_text("5700000\n", encoding="ascii")
            popen = mock.Mock()
            popen.return_value.poll.return_value = None
            io = vfdump.SysfsMeasureIO(root, popen=popen, which=lambda name: "/usr/bin/" + name)
            self.assertEqual(io.read_cap(3), 5_700_000)
            io.write_cap(3, 3_000_000)
            self.assertEqual((root / "cpu3" / "cpufreq" / "scaling_max_freq").read_text(encoding="ascii"), "3000000")
            self.assertIsNone(io.check())
            load = io.start_load([8, 9, 10, 11], 5.2)
            popen.assert_called_once_with(["stress-ng", "--cpu", "4", "--taskset", "8,9,10,11", "--timeout", "6s", "--quiet"])
            load.stop()
            popen.return_value.terminate.assert_called_once()
            self.assertIn("stress-ng", vfdump.SysfsMeasureIO(root, popen=popen, which=lambda name: None).check() or "")

    def test_core_temperature_comes_from_the_coretemp_hwmon_by_core_id(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root, hw = Path(tmp) / "cpu", Path(tmp) / "hwmon"
            (root / "cpu5" / "topology").mkdir(parents=True)
            (root / "cpu5" / "topology" / "core_id").write_text("3\n", encoding="ascii")
            for n, name in enumerate(("acpitz", "coretemp")):
                (hw / f"hwmon{n}").mkdir(parents=True)
                (hw / f"hwmon{n}" / "name").write_text(name + "\n", encoding="ascii")
            (hw / "hwmon0" / "temp1_label").write_text("Core 3\n", encoding="ascii")  # a decoy in another hwmon
            (hw / "hwmon0" / "temp1_input").write_text("99000\n", encoding="ascii")
            for k, (label, milli) in enumerate((("Package id 0", 70000), ("Core 2", 60000), ("Core 3", 61500)), start=1):
                (hw / "hwmon1" / f"temp{k}_label").write_text(label + "\n", encoding="ascii")
                (hw / "hwmon1" / f"temp{k}_input").write_text(f"{milli}\n", encoding="ascii")
            io = vfdump.SysfsMeasureIO(root, popen=mock.Mock(), which=lambda name: "x", hwmon_root=hw)
            self.assertEqual(io.read_temp_c(5), 61.5)
            self.assertIsNone(io.read_temp_c(6))  # no topology/core_id: no sensor, no exception
            (root / "cpu6" / "topology").mkdir(parents=True)
            (root / "cpu6" / "topology" / "core_id").write_text("9\n", encoding="ascii")
            self.assertIsNone(io.read_temp_c(6))  # no "Core 9" label (E-cores may have none)


class Helpers(unittest.TestCase):
    def test_parse_cpu_list(self) -> None:
        self.assertEqual(vfdump.parse_cpu_list("0-2,8,1"), [0, 1, 2, 8])
        for bad in ("", "a", "3-1", "-1", "1,,2"):
            with self.assertRaises(ValueError, msg=bad):
                vfdump.parse_cpu_list(bad)

    def test_the_default_selection(self) -> None:
        cores = vfdump.select_cpus(arl_s_285k(), None, False)
        self.assertEqual([c.cpu for c in cores], [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20])
        self.assertEqual(len(vfdump.select_cpus(arl_s_285k(), None, True)), 24)

    def test_known_points_and_the_request_budget(self) -> None:
        topo = arl_s_285k()
        self.assertEqual((vfdump.known_points("P"), vfdump.known_points("E")), (8, 6))
        self.assertEqual((vfdump.p2_cap("P"), vfdump.p2_cap("E")), (10, 8))
        reqs = vfdump.plan_requests(vfdump.select_cpus(topo, None, False), topo)
        self.assertEqual(len(reqs), 8 * 11 + 4 * 9)  # the budget is the worst case (the caps), not the normal walk
        self.assertTrue(all(r.cmd == 0x10 and r.p1 == 0 and r.data == 0 and r.p2 <= (10 if r.cpu < 8 else 8) for r in reqs))


class NeverTouchesTheRealSystem(unittest.TestCase):
    def test_the_default_sim_run_does_not_build_the_real_measure_io(self) -> None:
        # a --sim --measure run without an injected factory is refused before any I/O object is built (see Measure.test_measure_is_refused_in_the_simulator...)
        with mock.patch("arltuner.cli._default_measure_io") as real:
            case = Measure("test_measure_is_refused_in_the_simulator_without_injected_io")
            case.setUp()
            try:
                case.test_measure_is_refused_in_the_simulator_without_injected_io()
            finally:
                case.doCleanups()
            real.assert_not_called()


if __name__ == "__main__":
    unittest.main()
