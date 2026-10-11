"""G1 ops against the simulator: vf.dump (guarded, progress, one final response), audit.tail, telemetry.start/stop, cancel."""
from __future__ import annotations

import json
import unittest

from arltuner.guard import read_all
from arltuner.helper.protocol import MAX_LINE

from ._support import HelperCase, error_code


class PreflightOpTests(HelperCase):
    def test_result_is_the_report_plus_the_helpers_own_verdict(self) -> None:
        reply = self.start().call("p", "preflight")
        self.assertTrue(reply["ok"])
        result = reply["result"]
        self.assertEqual((result["platform"], result["mode"], result["writes_allowed"]), ("arl-s-285k", "write", True))
        self.assertEqual({c["status"] for c in result["checks"]}, {"pass", "missing"})


class VfDumpTests(HelperCase):
    def test_dump_one_p_core(self) -> None:
        h = self.start()
        reply = h.call("d1", "vf.dump", {"cpus": "0"})
        self.assertTrue(reply["ok"], reply)
        result = reply["result"]
        self.assertTrue(result["complete"])
        self.assertTrue(result["simulator"])
        (unit,) = result["units"]
        (cpu,) = unit["cpus"]
        self.assertEqual((cpu["cpu"], len(cpu["points"]), cpu["end"]), (0, 8, "rc 0x11 at P2=9"))
        self.assertEqual(result["ops_done"], 10)  # the OC word, 8 points, the rc 0x11 that ends the table
        # every request went through the guard: the audit log has one attempt per request
        attempts = [r for r in read_all(self.audit) if r["event"] == "attempt"]
        self.assertEqual(len(attempts), 10)
        self.assertEqual({r["cmd"] for r in attempts}, {"0x10"})  # only the read command ever left
        self.assertEqual(len(self.sim.requests), 10)

    def test_progress_events_then_exactly_one_final_response_then_state_idle(self) -> None:
        h = self.start()
        h.call("d1", "vf.dump", {"cpus": [0]})
        h.wait_for(lambda m: m.get("event") == "state" and m["state"] == "idle")
        mine = [m for m in h.messages if m.get("op_id") == "d1" or m.get("id") == "d1"]
        kinds = [m.get("event", "response") for m in mine]
        self.assertEqual(kinds[0], "state")
        self.assertEqual(kinds.count("response"), 1)
        self.assertEqual(kinds[-2:], ["response", "state"])
        progress = [m for m in mine if m.get("event") == "progress"]
        self.assertEqual(len(progress), 10)
        self.assertEqual([p["step"] for p in progress], list(range(1, 11)))
        self.assertEqual({p["of"] for p in progress}, {11})  # the worst case: P2 0..10
        self.assertEqual({p["ops_budget"] for p in progress}, {400})
        self.assertEqual([p["ops_used"] for p in progress][-1], 10)
        self.assertEqual(set(progress[0]), {"event", "op_id", "step", "of", "ops_used", "ops_budget"})
        self.assertEqual(mine[0]["state"], "planning")

    def test_state_is_planning_while_it_runs_and_idle_after(self) -> None:
        from ._support import BlockingSim

        self.sim = sim = BlockingSim(self.sim.config)
        sim.park_at = 2
        h = self.start()
        h.send({"id": "d", "op": "vf.dump", "args": {"cpus": "0"}})
        self.assertTrue(sim.parked.wait(10))
        h.wait_for(lambda m: m.get("event") == "heartbeat" and m["state"] == "planning" and m["op_id"] == "d")
        sim.release.set()
        self.assertTrue(h.response("d")["ok"])
        h.wait_for(lambda m: m.get("event") == "heartbeat" and m["state"] == "idle", start=len(h.messages) - 1)

    def test_full_default_dump_fits_the_line_cap(self) -> None:
        # all P-cores + one E CPU per cluster, then every E CPU: the largest result
        h = self.start()
        for rid, args in (("a", {}), ("b", {"all_e": True})):
            reply = h.call(rid, "vf.dump", args, timeout=60)
            self.assertTrue(reply["ok"], reply)
            self.assertLessEqual(len(json.dumps(reply["result"], separators=(",", ":"))), MAX_LINE)

    def test_unknown_cpu_is_refused_before_anything_is_sent(self) -> None:
        reply = self.start().call("d", "vf.dump", {"cpus": "99"})
        self.assertEqual(error_code(reply), "REFUSED")
        self.assertEqual(reply["error"]["detail"]["check"], "UNKNOWN_CORE")
        self.assertEqual(self.sim.wrmsr_count, 0)

    def test_default_policy_without_the_point_reads_refuses_with_the_allowlist_check(self) -> None:
        cfg = self.config(policy_path=None)  # the built-in conservative policy: no point reads, session cap 1
        reply = self.start(cfg).call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual(error_code(reply), "REFUSED")
        self.assertEqual(reply["error"]["detail"]["check"], "NOT_ALLOWLISTED")
        self.assertEqual(self.sim.wrmsr_count, 0)

    def test_bad_policy_file_is_refused(self) -> None:
        self.policy.write_text("this is not toml = [", encoding="utf-8")
        reply = self.start().call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual(error_code(reply), "REFUSED")
        self.assertEqual(reply["error"]["detail"]["check"], "POLICY_INVALID")

    def test_session_budget_is_checked_before_the_first_request(self) -> None:
        self.policy.write_text(self.policy.read_text().replace("max_operations_per_session = 300", "max_operations_per_session = 5"), encoding="utf-8")
        reply = self.start().call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual(reply["error"]["detail"]["check"], "SESSION_LIMIT")
        self.assertEqual(self.sim.wrmsr_count, 0)

    def test_per_boot_budget_counts_the_audit_log_of_every_process(self) -> None:
        self.policy.write_text(self.policy.read_text().replace("max_operations_per_boot = 400", "max_operations_per_boot = 15"), encoding="utf-8")
        h = self.start()
        self.assertTrue(h.call("a", "vf.dump", {"cpus": "0"})["ok"])  # 10 of 15
        second = h.call("b", "vf.dump", {"cpus": "0"})
        self.assertEqual(second["error"]["detail"]["check"], "BOOT_LIMIT")
        self.assertEqual(second["error"]["detail"]["left"], 5)
        self.assertEqual(len(self.sim.requests), 10)

    def test_unreadable_audit_log_refuses(self) -> None:
        self.audit.write_text("this is not an audit record\n{\"event\":\"attempt\"}\n", encoding="utf-8")
        reply = self.start().call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual(reply["error"]["detail"]["check"], "AUDIT_UNAVAILABLE")
        self.assertEqual(self.sim.wrmsr_count, 0)

    def test_a_foreign_mailbox_lock_is_a_guard_refusal_with_partial_result(self) -> None:
        self.lock.write_text("4242\n", encoding="utf-8")  # the CLI (or another process) is inside a transaction
        reply = self.start().call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual(error_code(reply), "REFUSED")
        self.assertEqual(reply["error"]["detail"]["check"], "LOCKED")
        self.assertEqual(reply["error"]["detail"]["partial"]["complete"], False)
        self.assertEqual(self.sim.wrmsr_count, 0)
        self.assertEqual(self.lock.read_text(), "4242\n")  # not ours: left alone

    def test_the_helper_never_sets_the_environment_flag(self) -> None:
        import os

        before = os.environ.get("ARLTUNER_ALLOW_MAILBOX")
        self.assertTrue(self.start().call("d", "vf.dump", {"cpus": "0"})["ok"])
        self.assertEqual(os.environ.get("ARLTUNER_ALLOW_MAILBOX"), before)

    def test_the_lock_file_is_removed_between_transactions_and_after(self) -> None:
        h = self.start()
        self.assertTrue(h.call("d", "vf.dump", {"cpus": "0"})["ok"])
        self.assertFalse(self.lock.exists())  # the guard's per-transaction lock; the helper's own lifetime lock is a sibling file
        self.assertTrue(self.lock.with_name(self.lock.name + ".helper").exists())


class AuditTailTests(HelperCase):
    def write_audit(self, n: int) -> None:
        self.audit.write_text("".join(json.dumps({"seq": i, "event": "attempt"}) + "\n" for i in range(n)), encoding="utf-8")

    def test_missing_log_is_empty(self) -> None:
        reply = self.start().call("t", "audit.tail")
        self.assertEqual(reply["result"]["records"], [])

    def test_last_n_records(self) -> None:
        self.write_audit(50)
        result = self.start().call("t", "audit.tail", {"n": 3})["result"]
        self.assertEqual([r["seq"] for r in result["records"]], [47, 48, 49])
        self.assertTrue(result["truncated"])

    def test_default_n_is_twenty_and_short_logs_are_not_truncated(self) -> None:
        self.write_audit(5)
        h = self.start()
        result = h.call("t", "audit.tail")["result"]
        self.assertEqual(len(result["records"]), 5)
        self.assertFalse(result["truncated"])
        self.write_audit(30)
        self.assertEqual(len(h.call("t2", "audit.tail")["result"]["records"]), 20)

    def test_unparsable_line_is_reported_not_fatal(self) -> None:
        self.audit.write_text('{"seq":1}\nnot json\n{"seq":3}\n', encoding="utf-8")
        records = self.start().call("t", "audit.tail", {"n": 5})["result"]["records"]
        self.assertEqual(records[1], {"unparsable": "not json"})

    def test_result_is_cut_to_fit_the_line_cap(self) -> None:
        big = "z" * 3000
        self.audit.write_text("".join(json.dumps({"seq": i, "pad": big}) + "\n" for i in range(200)), encoding="utf-8")
        h = self.start()
        reply = h.call("t", "audit.tail", {"n": 200})
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["result"]["truncated"])
        self.assertEqual(reply["result"]["records"][-1]["seq"], 199)  # the newest survive
        self.assertLess(len(reply["result"]["records"]), 200)

    def test_works_while_an_operation_runs(self) -> None:
        from ._support import BlockingSim

        self.sim = sim = BlockingSim(self.sim.config)
        sim.park_at = 1
        h = self.start()
        h.send({"id": "d", "op": "vf.dump", "args": {"cpus": "0"}})
        self.assertTrue(sim.parked.wait(10))
        self.assertTrue(h.call("t", "audit.tail")["ok"])
        self.assertTrue(h.call("p", "preflight")["ok"])
        sim.release.set()
        self.assertTrue(h.response("d")["ok"])


class TelemetryTests(HelperCase):
    def test_start_streams_events_and_stop_ends_them(self) -> None:
        h = self.start()
        reply = h.call("tel", "telemetry.start", {"interval_s": 0.2})
        self.assertEqual((reply["ok"], reply["result"]["running"], reply["result"]["op_id"]), (True, True, "tel"))
        first = h.wait_for(lambda m: m.get("event") == "telemetry")
        self.assertEqual(set(first), {"event", "op_id", "t", "values"})
        self.assertEqual(first["op_id"], "tel")
        self.assertIn("cores", first["values"])
        h.wait_for(lambda m: m.get("event") == "telemetry" and m["t"] != first["t"])  # keeps sampling
        self.assertEqual(h.call("tel-stop", "telemetry.stop")["result"]["stopped"], True)
        count = len(h.events("telemetry"))
        h.wait_for(lambda m: m.get("event") == "heartbeat", start=len(h.messages))
        h.wait_for(lambda m: m.get("event") == "heartbeat", start=len(h.messages))
        self.assertEqual(len(h.events("telemetry")), count)

    def test_second_start_attaches_to_the_running_stream(self) -> None:
        h = self.start()
        h.call("tel", "telemetry.start", {"interval_s": 0.2})
        again = h.call("tel2", "telemetry.start", {"interval_s": 0.2})  # the same interval: attach (a different one restarts the sampler, contract 9)
        self.assertEqual((again["result"]["already_running"], again["result"]["restarted"], again["result"]["op_id"]), (True, False, "tel"))
        h.call("s", "telemetry.stop")

    def test_stop_without_start_is_harmless(self) -> None:
        self.assertEqual(self.start().call("s", "telemetry.stop")["result"], {"stopped": False})

    def test_telemetry_never_touches_the_mailbox(self) -> None:
        h = self.start()
        h.call("tel", "telemetry.start", {"interval_s": 0.2})
        h.wait_for(lambda m: m.get("event") == "telemetry")
        h.wait_for(lambda m: m.get("event") == "telemetry" and m["t"] != h.events("telemetry")[0]["t"])
        h.call("s", "telemetry.stop")
        self.assertGreater(len(self.msr_for_telemetry.reads), 0)
        self.assertNotIn(0x150, {msr for _, msr in self.msr_for_telemetry.reads})  # the OC mailbox
        self.assertEqual(self.sim.wrmsr_count + self.sim.rdmsr_count, 0)  # the guarded port was never used by the sampler
        self.assertEqual(h.events("telemetry")[0]["values"]["limit_reasons_raw"]["status"], "unavailable")

    def test_telemetry_runs_alongside_a_dump(self) -> None:
        from ._support import BlockingSim

        self.sim = sim = BlockingSim(self.sim.config)
        sim.park_at = 1
        h = self.start()
        h.call("tel", "telemetry.start", {"interval_s": 0.2})
        h.send({"id": "d", "op": "vf.dump", "args": {"cpus": "0"}})
        self.assertTrue(sim.parked.wait(10))
        n = len(h.events("telemetry"))
        h.wait_for(lambda m: m.get("event") == "telemetry", start=n + 1 if n else 0)
        sim.release.set()
        self.assertTrue(h.response("d")["ok"])

    def test_a_sampler_that_cannot_start_is_an_internal_error(self) -> None:
        def boom(interval: float):
            raise RuntimeError("no sampler")

        reply = self.start(self.config(sampler_factory=boom)).call("tel", "telemetry.start")
        self.assertEqual(error_code(reply), "INTERNAL")


if __name__ == "__main__":
    unittest.main()
