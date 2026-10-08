"""D-027 c: the helper's own preflight is the authority. Any write op is refused unless the mode is 'write' with no UNKNOWN check; the server's
display-only preflight (and anything in the request) cannot change that. Tested with a stub write op and with vf.dump (a mailbox read is a WRMSR)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arltuner.helper import OpSpec, State
from arltuner.platform.preflight import Status, preflight

from ._support import GOOD_REPORT, READ_ONLY_REPORT, UNKNOWN_REPORT, HelperCase, error_code, report


def forbidden_port():
    raise AssertionError("a port must never be constructed when the preflight gate refuses")


class StubWriteOps:
    """Registers a long and a quick stub write op that record every run."""

    def __init__(self) -> None:
        self.ran: list[str] = []
        self.long = OpSpec("stub.write", lambda h, ctx, a: self.ran.append(ctx.req_id) or {"done": True}, lambda a: a, long=True, write=True, state=State.APPLYING)
        self.quick = OpSpec("stub.quick", lambda h, ctx, a: self.ran.append(ctx.req_id) or {"done": True}, lambda a: a, write=True)

    def extra(self) -> dict[str, OpSpec]:
        return {"stub.write": self.long, "stub.quick": self.quick}


class PreflightAuthorityTests(HelperCase):
    def setUp(self) -> None:
        super().setUp()
        self.stub = StubWriteOps()
        self.current = GOOD_REPORT

    def hardware_cfg(self, **kw):
        values = dict(simulator=False, preflight_fn=lambda: self.current, extra_ops=self.stub.extra(), health_gate_factory=lambda: (lambda phase: (True, "ok")))
        values.update(kw)
        return self.config(**values)

    def test_mode_write_without_unknown_runs_the_stub_write_op(self) -> None:
        h = self.start(self.hardware_cfg())
        self.assertTrue(h.call("w", "stub.write")["ok"])
        self.assertTrue(h.call("q", "stub.quick")["ok"])
        self.assertEqual(self.stub.ran, ["w", "q"])

    def test_unknown_check_refuses_every_write_op(self) -> None:
        self.current = UNKNOWN_REPORT  # e.g. the unprivileged-looking msr allow_writes
        h = self.start(self.hardware_cfg())
        for rid, op in (("w", "stub.write"), ("q", "stub.quick")):
            reply = h.call(rid, op)
            self.assertEqual(error_code(reply), "PREFLIGHT_FAILED")
            self.assertEqual(reply["error"]["detail"]["mode"], "write")  # the mode alone says 'write': the UNKNOWN is what blocks
            self.assertEqual([b["name"] for b in reply["error"]["detail"]["blockers"]], ["msr_allow_writes"])
            self.assertIn("UNKNOWN", reply["error"]["message"])
        self.assertEqual(self.stub.ran, [])

    def test_read_only_and_refuse_modes_refuse_every_write_op(self) -> None:
        for rep, mode in ((READ_ONLY_REPORT, "read-only"), (report(("python", Status.REFUSE)), "refuse")):
            with self.subTest(mode=mode):
                self.current = rep
                h = self.start(self.hardware_cfg())
                reply = h.call("w", "stub.write")
                self.assertEqual(error_code(reply), "PREFLIGHT_FAILED")
                self.assertEqual(reply["error"]["detail"]["mode"], mode)
                h.stop()  # releases the instance lock for the next sub-case
        self.assertEqual(self.stub.ran, [])

    def test_the_refusal_changes_no_state_and_sends_no_state_event(self) -> None:
        self.current = UNKNOWN_REPORT
        h = self.start(self.hardware_cfg())
        h.call("w", "stub.write")
        self.assertEqual(h.events("state"), [])
        self.assertEqual(h.call("c", "cancel")["result"]["state"], "idle")

    def test_the_gate_is_a_fresh_run_every_time(self) -> None:
        h = self.start(self.hardware_cfg())
        self.assertTrue(h.call("w1", "stub.write")["ok"])
        self.current = UNKNOWN_REPORT  # the machine changed between two requests
        self.assertEqual(error_code(h.call("w2", "stub.write")), "PREFLIGHT_FAILED")
        self.current = GOOD_REPORT
        self.assertTrue(h.call("w3", "stub.write")["ok"])
        self.assertEqual(self.stub.ran, ["w1", "w3"])

    def test_a_preflight_that_cannot_run_refuses(self) -> None:
        def broken():
            raise OSError("proc is gone")

        h = self.start(self.hardware_cfg(preflight_fn=broken))
        reply = h.call("w", "stub.write")
        self.assertEqual(error_code(reply), "PREFLIGHT_FAILED")
        self.assertIn("could not run", reply["error"]["message"])
        self.assertEqual(error_code(h.call("p", "preflight")), "PREFLIGHT_FAILED")  # the op itself reports it too

    def test_vf_dump_is_a_write_op_and_never_builds_a_port_when_refused(self) -> None:
        self.current = UNKNOWN_REPORT
        h = self.start(self.hardware_cfg(port_factory=forbidden_port))
        reply = h.call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual(error_code(reply), "PREFLIGHT_FAILED")
        self.assertEqual(self.sim.wrmsr_count, 0)

    def test_no_health_gate_on_hardware_refuses_before_the_port_exists(self) -> None:
        h = self.start(self.hardware_cfg(port_factory=forbidden_port, health_gate_factory=lambda: None))
        reply = h.call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual((error_code(reply), reply["error"]["detail"]["check"]), ("REFUSED", "NO_HEALTH_GATE"))

    def test_the_simulator_is_not_hardware_and_is_not_gated(self) -> None:
        self.current = UNKNOWN_REPORT
        h = self.start(self.config(preflight_fn=lambda: self.current, extra_ops=self.stub.extra()))  # simulator=True
        self.assertTrue(h.call("d", "vf.dump", {"cpus": "0"})["ok"])

    def test_nothing_in_a_request_can_change_the_verdict(self) -> None:
        self.current = UNKNOWN_REPORT
        h = self.start(self.hardware_cfg())
        for rid, extra in (("a", {"preflight": "ok"}), ("b", {"mode": "write"}), ("c", {"force": True}), ("d", {"root": "/tmp"})):
            self.assertEqual(error_code(h.call(rid, "preflight", extra)), "BAD_REQUEST")
        h.send({"id": "e", "op": "stub.write", "preflight": {"mode": "write", "checks": []}})
        self.assertEqual(error_code(h.response("e")), "BAD_REQUEST")
        self.assertEqual(self.stub.ran, [])

    def test_preflight_op_reports_the_helpers_verdict(self) -> None:
        h = self.start(self.hardware_cfg())
        self.assertTrue(h.call("p1", "preflight")["result"]["writes_allowed"])
        self.current = UNKNOWN_REPORT
        result = h.call("p2", "preflight")["result"]
        self.assertFalse(result["writes_allowed"])
        self.assertEqual(result["mode"], "write")  # the mode still says write; UNKNOWN is why writes are not allowed
        self.current = READ_ONLY_REPORT
        self.assertFalse(h.call("p3", "preflight")["result"]["writes_allowed"])

    def test_real_preflight_on_an_empty_root_is_not_write_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rep = preflight(Path(td), python_version=(3, 12, 0))
        self.assertNotEqual(rep.mode, "write")
        h = self.start(self.hardware_cfg(preflight_fn=lambda: rep))
        self.assertEqual(error_code(h.call("w", "stub.write")), "PREFLIGHT_FAILED")


if __name__ == "__main__":
    unittest.main()
