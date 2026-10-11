"""``stress.start`` / ``stress.stop`` / ``stress.list`` through a real helper on pipes, over FAKE tool binaries (recorded outputs), a fake cpufreq sysfs and the
SimModel. No real stress-ng / stressapptest, no sudo, no hardware, no mailbox."""
from __future__ import annotations

import json
import os
import signal
import stat
import time
import types
import unittest
from pathlib import Path
from typing import Any

from arltuner.health import HealthSnapshot
from arltuner.helper.ops_measure import CAP_ATTR, EPP_ATTR
from arltuner.helper.ops_stress import Invoker, InvokerError, StressWiring, default_invoker
from arltuner.helper.trust import TrustError
from arltuner.stress import CHILD_ENV, KernelLogWatch

from tests.stress._support import (
    FakeKlog, RecordingKillpg, RecordingPopen, alive, health_snapshot, make_fake, sim_sampler, stress_wiring,
)

from ._measure_support import ORIGINAL_EPP, MeasureCase
from ._support import Harness, error_code

RESULT_KEYS = {
    "id", "kind", "started", "ended", "duration_s", "elapsed_s", "stopped_by", "errors", "error_lines", "achieved", "cap_valid", "max_temp_c", "max_pkg_w",
    "mce_new", "health", "record_path",
}
START = {"kind": "cpu", "duration_s": 30}


class StressCase(MeasureCase):
    def setUp(self) -> None:
        super().setUp()
        self.popen = RecordingPopen()
        self.killpg = RecordingKillpg()
        self.klog = FakeKlog()

    def swiring(self, **overrides: Any) -> StressWiring:
        values: dict[str, Any] = dict(
            popen=self.popen, killpg=self.killpg, klog_factory=lambda: KernelLogWatch([self.klog]),
            sample_fn=sim_sampler(self.cpufreq, self.model),
        )
        values.update(overrides)
        return stress_wiring(self.tmp, **values)

    def start_stress(self, wiring: StressWiring | None = None, *, measure: bool = True) -> Harness:
        for old in self.harnesses:  # one helper at a time (the instance lock is per path)
            old.stop()
        h = Harness(self.config())
        h.helper.measure_wiring = self.wiring() if measure else None
        h.helper.stress_wiring = wiring or self.swiring()
        self.harnesses.append(h)
        h.start()
        return h

    def fake(self, name: str, recorded: str, rc: int = 0, **kw: Any) -> str:
        return make_fake(self.tmp, name, recorded, rc, **kw)

    def run_stress(self, h: Harness, args: dict[str, Any] | None = None, *, rid: str = "s") -> dict[str, Any]:
        return h.call(rid, "stress.start", {**START, **(args or {})}, timeout=30)

    def begin(self, h: Harness, args: dict[str, Any], rid: str = "s") -> None:
        h.send({"id": rid, "op": "stress.start", "args": args})

    def wait_pid(self, tool: str) -> int:
        pidfile = Path(tool + ".pid")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if pidfile.exists() and pidfile.read_text().strip().isdigit():
                return int(pidfile.read_text())
            time.sleep(0.02)
        raise AssertionError("the fake tool never wrote its grandchild pid")

    def wait_dead(self, pid: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not alive(pid):
                return True
            time.sleep(0.02)
        return False


# =============================================================================================================================================
class ValidationTests(StressCase):
    def test_bad_arguments_are_refused_before_anything_starts(self) -> None:
        h = self.start_stress()
        bad: list[dict[str, Any]] = [
            {}, {"kind": "disk", "duration_s": 60}, {"kind": "cpu"}, {"kind": "cpu", "duration_s": 29}, {"kind": "cpu", "duration_s": 3601},
            {"kind": "cpu", "duration_s": "60"}, {"kind": "cpu", "duration_s": True}, {"kind": "cpu", "duration_s": 60.0},
            {"kind": "cpu", "duration_s": 60, "method": "int64"}, {"kind": "cpu", "duration_s": 60, "method": "avx"},
            {"kind": "cpu", "duration_s": 60, "units": []}, {"kind": "cpu", "duration_s": 60, "units": ["P9"]}, {"kind": "cpu", "duration_s": 60, "units": ["P0", "P0"]},
            {"kind": "cpu", "duration_s": 60, "units": "P0"}, {"kind": "cpu", "duration_s": 60, "ratio_cap": 7}, {"kind": "cpu", "duration_s": 60, "ratio_cap": 61},
            {"kind": "cpu", "duration_s": 60, "ratio_cap": "40"}, {"kind": "cpu", "duration_s": 60, "ratio_cap": True}, {"kind": "cpu", "duration_s": 60, "ratio_cap": 40.0},
            {"kind": "cpu", "duration_s": 60, "memory_mb": 1024}, {"kind": "cpu", "duration_s": 60, "taskset": "0"},
            {"kind": "memory", "duration_s": 60, "method": "integer"}, {"kind": "memory", "duration_s": 60, "units": ["P0"]},
            {"kind": "memory", "duration_s": 60, "memory_mb": 255}, {"kind": "memory", "duration_s": 60, "memory_mb": "1024"}, {"kind": "memory", "duration_s": 60, "memory_mb": True},
        ]
        for i, args in enumerate(bad):
            with self.subTest(args=args):
                self.assertEqual(error_code(h.call(f"b{i}", "stress.start", args)), "BAD_REQUEST")
        self.assertEqual(self.popen.calls, [])
        self.assertEqual(h.helper.machine.state.value, "idle")

    def test_stop_and_list_take_no_arguments(self) -> None:
        h = self.start_stress()
        self.assertEqual(error_code(h.call("a", "stress.stop", {"op_id": "x"})), "BAD_REQUEST")
        self.assertEqual(error_code(h.call("b", "stress.list", {"n": 1})), "BAD_REQUEST")

    def test_defaults_are_the_integer_method_on_all_twelve_units(self) -> None:
        h = self.start_stress()
        reply = self.run_stress(h)
        self.assertTrue(reply["ok"], reply)
        cmd, _ = self.popen.calls[0]
        self.assertEqual(cmd[1:], ["--cpu", "24", "--taskset", "0-23", "--cpu-method", "int64", "--verify", "--metrics-brief", "--timeout", "30s"])
        record = json.loads(Path(reply["result"]["record_path"]).read_text())
        self.assertEqual((record["method"], record["units"], record["ratio_cap"], record["memory_mb"]), ("integer", [f"P{n}" for n in range(8)] + [f"EC{n}" for n in range(4)], None, None))


class InvokerTests(unittest.TestCase):
    @staticmethod
    def pw(uid: int = 1000, gid: int = 1001, name: str = "yk") -> Any:
        return types.SimpleNamespace(pw_uid=uid, pw_gid=gid, pw_name=name)

    def test_the_invoking_user_comes_from_pkexec_uid(self) -> None:
        self.assertEqual(default_invoker({"PKEXEC_UID": "1000"}, lambda uid: self.pw(uid)), Invoker(1000, 1001, "yk"))

    def test_anything_else_is_refused(self) -> None:
        def missing(uid: int) -> Any:
            raise KeyError(uid)

        for env in ({}, {"PKEXEC_UID": ""}, {"PKEXEC_UID": "abc"}, {"PKEXEC_UID": "-1"}, {"PKEXEC_UID": "1000 "}, {"PKEXEC_UID": "0"}, {"PKEXEC_UID": "00"}, {"PKEXEC_UID": "1" * 11}):
            with self.subTest(env=env), self.assertRaises(InvokerError):
                default_invoker(env, lambda uid: self.pw(uid))
        with self.assertRaises(InvokerError):
            default_invoker({"PKEXEC_UID": "1234"}, missing)
        with self.assertRaises(InvokerError):
            default_invoker({"PKEXEC_UID": "1234"}, lambda uid: self.pw(uid + 1))  # the database answered for someone else


class NoInvokerTests(StressCase):
    def test_a_missing_invoker_refuses_the_run(self) -> None:
        def nobody() -> Invoker:
            raise InvokerError("PKEXEC_UID is not set")

        h = self.start_stress(self.swiring(invoker_fn=nobody))
        reply = self.run_stress(h)
        self.assertEqual(error_code(reply), "REFUSED")
        self.assertEqual(reply["error"]["detail"]["check"], "NO_INVOKER")
        self.assertEqual(self.popen.calls, [])
        self.assertEqual(h.helper.machine.state.value, "idle")


# =============================================================================================================================================
class RunTests(StressCase):
    def test_a_cpu_run_gives_the_contract_result(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok", delay=0.3)
        h = self.start_stress(self.swiring(stress_ng=tool))
        reply = self.run_stress(h, {"units": ["EC1", "P0"], "method": "integer"})
        self.assertTrue(reply["ok"], reply)
        r = reply["result"]
        self.assertEqual(set(r), RESULT_KEYS)
        self.assertEqual((r["kind"], r["duration_s"], r["stopped_by"], r["errors"], r["error_lines"], r["mce_new"], r["health"], r["cap_valid"]), ("cpu", 30, "time", 0, [], 0, "clean", None))
        self.assertRegex(r["started"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertRegex(r["ended"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertGreaterEqual(r["elapsed_s"], 0.2)
        self.assertEqual(list(r["achieved"]), ["P0", "EC1"], "in unit order, only the units that ran")
        self.assertEqual(r["achieved"]["P0"]["ratio_mode"], 57)
        self.assertEqual(r["achieved"]["EC1"]["ratio_mode"], 46)
        self.assertAlmostEqual(r["achieved"]["P0"]["mhz_mean"], 5701.0, delta=0.5)
        self.assertEqual(r["max_temp_c"], 75.5)
        self.assertGreater(r["max_pkg_w"], 180)
        # the stored record
        path = Path(r["record_path"])
        self.assertEqual(path.name, r["id"] + ".json")
        record = json.loads(path.read_text())
        self.assertEqual({k: record[k] for k in r if k != "record_path"}, {k: v for k, v in r.items() if k != "record_path"})
        self.assertEqual((record["method"], record["units"], record["returncode"]), ("integer", ["P0", "EC1"], 0))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        # nothing says pass
        self.assertNotIn("pass", json.dumps(r).lower())

    def test_the_child_is_started_as_the_user_in_its_own_group_with_a_fixed_environment(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok")
        link = self.tmp / "link-to-stress-ng"
        link.symlink_to(tool)
        seen: list[str] = []
        h = self.start_stress(self.swiring(stress_ng=str(link), invoker_fn=lambda: Invoker(1234, 5678, "operator"), trust_check=seen.append))
        self.assertTrue(self.run_stress(h, {"units": ["P0", "P1", "EC0"], "method": "vector-math"})["ok"])
        cmd, kw = self.popen.calls[0]
        self.assertEqual(cmd[0], os.path.realpath(tool), "argv[0] is the resolved executable")
        self.assertEqual(seen, [os.path.realpath(tool)] * 2, "the resolved path is trust-checked before the spawn (twice: early refusal, then right before)")
        self.assertEqual(cmd[1:], ["--cpu", "6", "--taskset", "0-1,8-11", "--cpu-method", "matrixprod", "--verify", "--metrics-brief", "--timeout", "30s"])
        self.assertEqual((kw["user"], kw["group"], kw["extra_groups"]), (1234, 5678, []), "setgroups([]) / setgid / setuid in the child, before exec")
        self.assertIs(kw["start_new_session"], True)
        self.assertEqual((kw["cwd"], kw["env"], kw["shell"], kw["close_fds"]), ("/", CHILD_ENV, False, True))
        self.assertIsNot(kw["env"], CHILD_ENV)
        self.assertEqual(kw["stdin"], -3)  # subprocess.DEVNULL

    def test_state_and_progress_events(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok", delay=0.4)
        h = self.start_stress(self.swiring(stress_ng=tool))
        self.assertTrue(self.run_stress(h)["ok"])
        states = [m["state"] for m in h.events("state")]
        self.assertIn("stressing", states)
        self.assertEqual(states[-1], "idle")
        progress = [m for m in h.events("progress") if "elapsed_s" in m]
        self.assertGreaterEqual(len(progress), 3, "an event every progress interval")
        self.assertEqual(set(progress[0]), {"event", "op_id", "elapsed_s", "of_s", "errors"})
        self.assertEqual((progress[0]["op_id"], progress[0]["of_s"], progress[0]["errors"]), ("s", 30, 0))
        self.assertEqual([m["elapsed_s"] for m in progress], sorted(m["elapsed_s"] for m in progress))

    def test_a_verification_failure(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-verify-fail", 2, delay=0.2)
        h = self.start_stress(self.swiring(stress_ng=tool))
        r = self.run_stress(h, {"method": "vector-math"})["result"]
        self.assertEqual((r["stopped_by"], r["errors"], r["health"]), ("error", 1, "clean"))
        self.assertIn("matrixprod: data check failure", r["error_lines"][0])
        self.assertEqual(self.list_rows(h)[0]["errors"], 1)

    def test_a_tool_that_crashes_without_a_verdict(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok", 127)
        h = self.start_stress(self.swiring(stress_ng=tool))
        r = self.run_stress(h)["result"]
        self.assertEqual((r["stopped_by"], r["errors"]), ("error", 0))
        self.assertTrue(any("ended abnormally (exit code 127)" in line for line in r["error_lines"]), r["error_lines"])

    def test_a_memory_run(self) -> None:
        h = self.start_stress()
        r = self.run_stress(h, {"kind": "memory", "duration_s": 60})["result"]
        self.assertEqual((r["kind"], r["stopped_by"], r["errors"], r["health"]), ("memory", "time", 0, "clean"))
        cmd, _ = self.popen.calls[0]
        self.assertEqual(cmd[1:], ["-s", "60", "-M", str((16384 - 2048) * 3 // 4), "-W"], "default: three quarters of MemAvailable - 2048 MiB")
        self.assertEqual(len(r["achieved"]), 12)

    def test_a_memory_miscompare(self) -> None:
        tool = self.fake("stressapptest", "stressapptest-miscompare", 1)
        h = self.start_stress(self.swiring(stressapptest=tool))
        r = self.run_stress(h, {"kind": "memory", "memory_mb": 2048})["result"]
        self.assertEqual((r["stopped_by"], r["errors"]), ("error", 2))
        self.assertTrue(any("miscompare on CPU 3" in line for line in r["error_lines"]))
        self.assertEqual(self.popen.calls[0][0][1:], ["-s", "30", "-M", "2048", "-W"])

    def test_the_memory_size_is_checked_against_mem_available(self) -> None:
        h = self.start_stress(self.swiring(meminfo_fn=lambda: 8192))
        top = 8192 - 2048
        over = self.run_stress(h, {"kind": "memory", "memory_mb": top + 1})
        self.assertEqual(error_code(over), "BAD_REQUEST")
        self.assertEqual(over["error"]["detail"]["check"], "MEM_RANGE")
        self.assertTrue(self.run_stress(h, {"kind": "memory", "memory_mb": top}, rid="edge")["ok"])
        self.assertEqual(self.popen.calls[0][0][4], str(top))
        tight = self.start_stress(self.swiring(meminfo_fn=lambda: 2048 + 255))
        low = self.run_stress(tight, {"kind": "memory"})
        self.assertEqual((error_code(low), low["error"]["detail"]["check"]), ("REFUSED", "MEM_RANGE"))

    def test_a_missing_or_untrusted_tool_is_not_started(self) -> None:
        h = self.start_stress(self.swiring(stress_ng=str(self.tmp / "no-such-stress-ng")))
        gone = self.run_stress(h)
        self.assertEqual((error_code(gone), gone["error"]["detail"]["check"]), ("REFUSED", "TOOL_MISSING"))
        self.assertIn("install the stress-ng package", gone["error"]["message"])

        def untrusted(path: str) -> None:
            raise TrustError(path, "owner uid 1000 is not root")

        h2 = self.start_stress(self.swiring(trust_check=untrusted))
        bad = self.run_stress(h2)
        self.assertEqual((error_code(bad), bad["error"]["detail"]["check"]), ("REFUSED", "TRUST_PATH"))
        self.assertEqual(self.popen.calls, [])

    def test_a_spawn_failure_is_a_refusal(self) -> None:
        def boom(cmd: list[str], **kw: Any) -> Any:
            raise OSError(2, "No such file or directory")

        h = self.start_stress(self.swiring(popen=boom))
        reply = self.run_stress(h)
        self.assertEqual((error_code(reply), reply["error"]["detail"]["check"]), ("REFUSED", "SPAWN"))
        self.assertEqual(h.helper.machine.state.value, "idle")

    def test_the_helper_stops_a_child_that_outlives_its_own_timeout(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok", delay=30)
        h = self.start_stress(self.swiring(stress_ng=tool, grace_s=-29.5))  # duration 30 s + (-29.5 s): the helper gives up at 0.5 s
        r = self.run_stress(h)["result"]
        self.assertEqual(r["stopped_by"], "error")
        self.assertTrue(any("did not end by its own timeout" in line for line in r["error_lines"]), r["error_lines"])
        self.assertLess(r["elapsed_s"], 10)
        self.assertTrue(self.killpg.calls)

    def test_stop_with_nothing_running(self) -> None:
        h = self.start_stress()
        self.assertEqual(h.call("x", "stress.stop")["result"], {"cancelled": False, "state": "idle"})

    def list_rows(self, h: Harness) -> list[dict[str, Any]]:
        return h.call("list", "stress.list")["result"]["results"]


# =============================================================================================================================================
class HealthBracketTests(StressCase):
    def test_new_mce_lines_and_a_health_delta(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok")
        self.klog.lines = ["mce: [Hardware Error]: Machine check events logged", "usb 1-1: new device", "Write to unrecognized MSR 0x1a0"]
        shots = iter([health_snapshot(), health_snapshot(modules=frozenset({"msr", "x_new_module"}))])
        h = self.start_stress(self.swiring(stress_ng=tool, health_fn=lambda: next(shots)))
        r = self.run_stress(h)["result"]
        self.assertEqual((r["mce_new"], r["health"], r["errors"], r["stopped_by"]), (1, "delta", 0, "time"))
        text = "\n".join(r["error_lines"])
        self.assertIn("1 new MCE / hardware-error kernel line", text)
        self.assertIn("Machine check events logged", text)
        self.assertIn("health UNCLEAN: modules added: x_new_module", text)

    def test_a_clean_bracket(self) -> None:
        h = self.start_stress()
        r = self.run_stress(h)["result"]
        self.assertEqual((r["mce_new"], r["health"]), (0, "clean"))

    def test_an_unreadable_kernel_log_or_snapshot_is_not_called_clean(self) -> None:
        self.klog.fail = True
        h = self.start_stress()
        r = self.run_stress(h)["result"]
        self.assertEqual(r["health"], "delta")
        self.assertTrue(any("kernel log could not be read" in line for line in r["error_lines"]))
        self.klog.fail = False

        def broken() -> HealthSnapshot:
            raise OSError("no /proc")

        h2 = self.start_stress(self.swiring(health_fn=broken))
        r2 = self.run_stress(h2)["result"]
        self.assertEqual(r2["health"], "delta")
        self.assertTrue(any("health snapshot could not be taken" in line for line in r2["error_lines"]))


# =============================================================================================================================================
class CancelTests(StressCase):
    def test_cancel_kills_the_whole_process_group(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok", hang=True)
        h = self.start_stress(self.swiring(stress_ng=tool))
        self.begin(h, {**START, "duration_s": 600})
        grandchild = self.wait_pid(tool)
        h.wait_for(lambda m: m.get("event") == "progress" and "elapsed_s" in m)
        self.assertTrue(alive(grandchild))
        self.assertEqual(h.helper.machine.state.value, "stressing")
        stop = h.call("stop", "stress.stop")
        self.assertEqual((stop["result"]["cancelled"], stop["result"]["op_id"], stop["result"]["state"]), (True, "s", "stressing"))
        r = h.response("s", 30)["result"]
        self.assertEqual(r["stopped_by"], "cancel")
        self.assertTrue(self.wait_dead(grandchild), "the grandchild in the child's process group is gone")
        pgid, sig = self.killpg.calls[0]
        self.assertEqual(sig, int(signal.SIGTERM))
        self.assertNotEqual(pgid, os.getpgrp(), "the child's group, never the helper's own")
        self.assertEqual(h.helper.machine.state.value, "idle")
        self.assertEqual(self.list_one(h)["stopped_by"], "cancel")
        self.assert_cpufreq_pristine()

    def test_a_child_that_ignores_term_is_killed(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok", hang=True, ignore_term=True)
        h = self.start_stress(self.swiring(stress_ng=tool, term_wait_s=0.3))
        self.begin(h, {**START, "duration_s": 600})
        grandchild = self.wait_pid(tool)
        time.sleep(0.2)
        h.call("stop", "stress.stop")
        r = h.response("s", 30)["result"]
        self.assertEqual(r["stopped_by"], "cancel")
        self.assertEqual([sig for _, sig in self.killpg.calls][:2], [int(signal.SIGTERM), int(signal.SIGKILL)])
        self.assertTrue(self.wait_dead(grandchild))

    def test_the_generic_cancel_op_works_too(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok", hang=True)
        h = self.start_stress(self.swiring(stress_ng=tool))
        self.begin(h, {**START, "duration_s": 600})
        grandchild = self.wait_pid(tool)
        self.assertTrue(h.call("c", "cancel", {"op_id": "s"})["result"]["cancelled"])
        self.assertEqual(h.response("s", 30)["result"]["stopped_by"], "cancel")
        self.assertTrue(self.wait_dead(grandchild))

    def test_a_helper_that_is_told_to_exit_stops_the_child(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok", hang=True)
        h = self.start_stress(self.swiring(stress_ng=tool))
        self.begin(h, {**START, "duration_s": 600, "units": ["P2"], "ratio_cap": 40})
        grandchild = self.wait_pid(tool)
        self.assertEqual(self.read(2, CAP_ATTR), "4000000")
        h.close_stdin()
        h.join(30)
        self.assertTrue(self.wait_dead(grandchild))
        self.assert_cpufreq_pristine()

    def test_nothing_else_can_start_while_a_stress_run_is_going(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok", hang=True)
        h = self.start_stress(self.swiring(stress_ng=tool))
        self.begin(h, {**START, "duration_s": 600})
        self.wait_pid(tool)
        for i, (op, args) in enumerate([("stress.start", START), ("vf.measure", {"mode": "quick"})]):
            busy = h.call(f"b{i}", op, args)
            self.assertEqual(error_code(busy), "BUSY", op)
            self.assertEqual(busy["error"]["detail"]["state"], "stressing")
        h.call("stop", "stress.stop")
        h.response("s", 30)

    def list_one(self, h: Harness) -> dict[str, Any]:
        return h.call("list", "stress.list")["result"]["results"][0]


# =============================================================================================================================================
class CapTests(StressCase):
    def test_the_cap_is_journaled_first_applied_and_restored(self) -> None:
        tool = self.fake("stress-ng", "stress-ng-ok", hang=True)
        h = self.start_stress(self.swiring(stress_ng=tool))
        self.begin(h, {**START, "duration_s": 600, "units": ["P2"], "ratio_cap": 40})
        self.wait_pid(tool)
        self.assertTrue(self.journal.exists(), "the measure journal is in force during the run")
        self.assertIs(self.cpufreq.journal_existed_at_first_write, True, "journaled before the first cpufreq write")
        self.assertEqual((self.read(2, CAP_ATTR), self.read(2, EPP_ATTR)), ("4000000", "performance"))
        self.assertEqual((self.read(3, CAP_ATTR), self.read(3, EPP_ATTR)), (str(self.model.max_khz(3)), ORIGINAL_EPP), "only the selected unit is touched")
        doc = json.loads(self.journal.read_text())
        self.assertEqual({(e["cpu"], e["attr"], e["value"]) for e in doc["entries"]}, {(2, CAP_ATTR, "5700000"), (2, EPP_ATTR, ORIGINAL_EPP)})
        h.call("stop", "stress.stop")
        r = h.response("s", 30)["result"]
        self.assertEqual(r["stopped_by"], "cancel")
        self.assert_cpufreq_pristine()

    def test_cap_valid_within_one_bin(self) -> None:
        h = self.start_stress()
        r = self.run_stress(h, {"units": ["P2"], "ratio_cap": 40})["result"]
        self.assertEqual((r["cap_valid"], r["achieved"]["P2"]["ratio_mode"], r["stopped_by"]), (True, 40, "time"))
        self.assert_cpufreq_pristine()
        # E-cores settle one bin low for caps in 16..29 (intel_pstate quirk, T-027): still within a bin
        r2 = self.run_stress(h, {"units": ["EC0"], "ratio_cap": 20}, rid="e")["result"]
        self.assertEqual((r2["cap_valid"], r2["achieved"]["EC0"]["ratio_mode"]), (True, 19))
        self.assert_cpufreq_pristine()

    def test_cap_invalid_when_the_unit_settles_more_than_a_bin_away(self) -> None:
        low = self.start_stress(self.swiring(sample_fn=sim_sampler(self.cpufreq, self.model, lambda cpu: 36 if cpu == 2 else None)))
        r = self.run_stress(low, {"units": ["P2", "P3"], "ratio_cap": 40})["result"]
        self.assertIs(r["cap_valid"], False)
        self.assertEqual({u: a["ratio_mode"] for u, a in r["achieved"].items()}, {"P2": 36, "P3": 40})
        self.assert_cpufreq_pristine()
        # a cap above what a non-favored P-core reaches (55x): the turbo table binds first -> invalid, not silently fine
        h = self.start_stress()
        r2 = self.run_stress(h, {"units": ["P2"], "ratio_cap": 57})["result"]
        self.assertEqual((r2["cap_valid"], r2["achieved"]["P2"]["ratio_mode"]), (False, 55))

    def test_no_cap_means_no_cpufreq_write_and_no_journal(self) -> None:
        h = self.start_stress()
        r = self.run_stress(h, {"units": ["P2"]})["result"]
        self.assertIsNone(r["cap_valid"])
        self.assertEqual(self.cpufreq.writes, [])
        self.assertFalse(self.journal.exists())

    def test_restored_on_a_failed_spawn_and_after_a_verification_failure(self) -> None:
        def boom(cmd: list[str], **kw: Any) -> Any:
            raise OSError(13, "Permission denied")

        h = self.start_stress(self.swiring(popen=boom))
        self.assertEqual(error_code(self.run_stress(h, {"units": ["P2"], "ratio_cap": 40})), "REFUSED")
        self.assert_cpufreq_pristine()
        bad = self.fake("stress-ng", "stress-ng-verify-fail", 2)
        h2 = self.start_stress(self.swiring(stress_ng=bad))
        self.assertEqual(self.run_stress(h2, {"units": ["P2"], "ratio_cap": 40}, rid="v")["result"]["errors"], 1)
        self.assert_cpufreq_pristine()

    def test_a_cap_write_that_fails_refuses_and_restores(self) -> None:
        self.cpufreq.fail = lambda cpu, attr, value: cpu == 3 and attr == CAP_ATTR and value == "4000000"
        h = self.start_stress()
        reply = self.run_stress(h, {"units": ["P2", "P3"], "ratio_cap": 40})
        self.assertEqual((error_code(reply), reply["error"]["detail"]["check"]), ("REFUSED", "CAP_WRITE"))
        self.cpufreq.fail = None
        self.assert_cpufreq_pristine()
        self.assertEqual(self.popen.calls, [])

    def test_a_cap_run_of_the_memory_screen_covers_every_cpu(self) -> None:
        h = self.start_stress()
        r = self.run_stress(h, {"kind": "memory", "ratio_cap": 30})["result"]
        self.assertTrue(r["cap_valid"])
        self.assertEqual({c for c, a, _ in self.cpufreq.writes if a == CAP_ATTR}, set(range(24)))
        self.assert_cpufreq_pristine()

    def test_a_cap_needs_the_measurement_support(self) -> None:
        h = self.start_stress(measure=False)
        reply = self.run_stress(h, {"ratio_cap": 40})
        self.assertEqual((error_code(reply), reply["error"]["detail"]["check"]), ("REFUSED", "NO_CPUFREQ"))
        self.assertTrue(self.run_stress(h, {}, rid="ok")["ok"], "an uncapped run needs no cpufreq")

    def test_a_restore_that_fails_is_loud_and_the_journal_stays_for_the_next_start(self) -> None:
        self.cpufreq.fail = lambda cpu, attr, value: cpu == 2 and attr == CAP_ATTR and value == "5700000"  # the restore write
        h = self.start_stress()
        reply = self.run_stress(h, {"units": ["P2"], "ratio_cap": 40})
        self.assertEqual(error_code(reply), "INTERNAL")
        self.assertIn("could NOT restore", reply["error"]["message"])
        self.assertTrue(self.journal.exists(), "the journal is kept: not everything could be restored")
        self.assertEqual(self.read(2, CAP_ATTR), "4000000")
        # while it cannot be applied, no new capped run starts
        calls = len(self.popen.calls)
        stale = self.run_stress(h, {"units": ["P2"], "ratio_cap": 40}, rid="again")
        self.assertEqual((error_code(stale), stale["error"]["detail"]["check"]), ("REFUSED", "STALE_JOURNAL"))
        self.assertEqual(len(self.popen.calls), calls)
        # once it can be, the next capped run applies the stale journal first and runs normally
        self.cpufreq.fail = None
        ok = self.run_stress(h, {"units": ["P2"], "ratio_cap": 41}, rid="third")
        self.assertTrue(ok["ok"], ok)
        self.assertTrue(any("stale measure journal applied" in line for line in self.logs), self.logs)
        self.assert_cpufreq_pristine()


# =============================================================================================================================================
class ListTests(StressCase):
    def test_list_is_newest_first_summary_rows(self) -> None:
        h = self.start_stress()
        ids = []
        for i in range(3):
            ids.append(self.run_stress(h, {"duration_s": 30 + i}, rid=f"r{i}")["result"]["id"])
            time.sleep(1.01)  # ids start with the second
        rows = h.call("l", "stress.list")["result"]["results"]
        self.assertEqual([r["id"] for r in rows], ids[::-1])
        self.assertEqual(set(rows[0]), {"id", "kind", "started", "ended", "duration_s", "elapsed_s", "stopped_by", "errors", "cap_valid", "health", "mce_new",
                                         "max_temp_c", "max_pkg_w", "method", "units", "ratio_cap", "memory_mb"})
        self.assertEqual([r["duration_s"] for r in rows], [32, 31, 30])

    def test_an_empty_store(self) -> None:
        self.assertEqual(self.start_stress().call("l", "stress.list")["result"], {"results": []})


# =============================================================================================================================================
class ServerAllowlistTests(unittest.TestCase):
    def test_the_server_op_table_mirrors_the_contract(self) -> None:
        from arltuner.web.server import OPS

        check, long_ = OPS["stress.start"]
        self.assertTrue(long_)
        for good in (
            {"kind": "cpu", "duration_s": 30}, {"kind": "memory", "duration_s": 3600, "memory_mb": 1024, "ratio_cap": None},
            {"kind": "cpu", "duration_s": 60, "method": "cache", "units": ["P0", "EC3"], "ratio_cap": 53},
        ):
            self.assertTrue(check(good), good)
        for bad in (
            {}, {"kind": "cpu"}, {"kind": "disk", "duration_s": 60}, {"kind": "cpu", "duration_s": 29}, {"kind": "cpu", "duration_s": True},
            {"kind": "cpu", "duration_s": 60, "method": "sse"}, {"kind": "cpu", "duration_s": 60, "units": ["P9"]}, {"kind": "cpu", "duration_s": 60, "units": []},
            {"kind": "cpu", "duration_s": 60, "ratio_cap": 61}, {"kind": "cpu", "duration_s": 60, "ratio_cap": "40"}, {"kind": "cpu", "duration_s": 60, "x": 1},
            {"kind": "memory", "duration_s": 60, "memory_mb": 10}, {"kind": "memory", "duration_s": 60, "memory_mb": 1.5},
        ):
            self.assertFalse(check(bad), bad)
        for op in ("stress.stop", "stress.list"):
            check, long_ = OPS[op]
            self.assertFalse(long_)
            self.assertTrue(check({}))
            self.assertFalse(check({"x": 1}))


# =============================================================================================================================================
class SimTests(MeasureCase):
    """``--sim``: no stress_wiring is attached, so the helper builds the simulator one from its measure wiring (the in-memory cpufreq and the SimModel)."""

    def start_sim(self) -> Harness:
        from arltuner.helper.ops_measure import sim_wiring

        h = Harness(self.config())
        h.helper.measure_wiring = sim_wiring(self.tmp / "state")
        self.harnesses.append(h)
        h.start()
        return h

    def test_a_simulated_capped_run_end_to_end(self) -> None:
        h = self.start_sim()
        reply = h.call("s", "stress.start", {"kind": "cpu", "duration_s": 30, "units": ["P0", "EC0"], "ratio_cap": 40}, timeout=60)
        self.assertTrue(reply["ok"], reply)
        r = reply["result"]
        self.assertEqual((r["stopped_by"], r["errors"], r["health"], r["cap_valid"], r["mce_new"]), ("time", 0, "clean", True, 0))
        self.assertEqual(r["achieved"]["P0"]["ratio_mode"], 40)
        self.assertLess(r["elapsed_s"], 40)
        self.assertTrue(str(r["record_path"]).startswith(str(self.tmp / "state" / "stress")))
        cpufreq = h.helper.measure_wiring.cpufreq
        self.assertEqual((cpufreq.read(0, CAP_ATTR), cpufreq.read(0, EPP_ATTR)), ("5700000", "balance_power"), "the simulated cpufreq is back as it was")
        self.assertFalse(h.helper.measure_wiring.journal_path.exists())
        self.assertEqual(h.call("l", "stress.list")["result"]["results"][0]["id"], r["id"])

    def test_a_simulated_memory_run_can_be_cancelled(self) -> None:
        h = self.start_sim()
        h.send({"id": "m", "op": "stress.start", "args": {"kind": "memory", "duration_s": 600}})
        h.wait_for(lambda m: m.get("event") == "progress" and "elapsed_s" in m)
        self.assertTrue(h.call("stop", "stress.stop")["result"]["cancelled"])
        r = h.response("m", 30)["result"]
        self.assertEqual((r["kind"], r["stopped_by"]), ("memory", "cancel"))


if __name__ == "__main__":
    unittest.main()
