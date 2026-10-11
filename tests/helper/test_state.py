"""State machine, conflicting requests, re-attach by id, cancel between transactions, EOF / shutdown, the machine-wide instance lock."""
from __future__ import annotations

import io
import os
import threading
import time
import unittest

from arltuner.helper import Helper, InstanceLock, OpSpec, State
from arltuner.helper.core import EXIT_INSTANCE_BUSY
from arltuner.helper.state import Busy, StateMachine

from ._support import BlockingSim, Harness, HelperCase, error_code


class MachineTests(unittest.TestCase):
    def test_begin_end_and_busy(self) -> None:
        m = StateMachine()
        self.assertEqual(m.state, State.IDLE)
        m.begin("a", "vf.dump", State.PLANNING)
        self.assertEqual((m.state, m.running.op_id), (State.PLANNING, "a"))
        with self.assertRaises(Busy) as ctx:
            m.begin("b", "apply", State.APPLYING)
        self.assertEqual(ctx.exception.running.state, State.PLANNING)
        m.end("other")  # only the owner ends it
        self.assertEqual(m.state, State.PLANNING)
        m.end("a")
        self.assertEqual(m.state, State.IDLE)

    def test_the_five_spec_states_and_measuring(self) -> None:
        self.assertEqual([s.value for s in State], ["idle", "planning", "applying", "capped", "stressing", "measuring"])  # measuring: G-111, contract 9

    def test_idle_is_not_a_long_op_state(self) -> None:
        with self.assertRaises(ValueError):
            StateMachine().begin("a", "x", State.IDLE)


class ParkedDump(HelperCase):
    """A vf.dump parked inside mailbox request number ``park_at`` (the transaction is in flight)."""

    park_at = 2

    def setUp(self) -> None:
        super().setUp()
        self.sim = BlockingSim(self.sim.config)
        self.sim.park_at = self.park_at

    def start_parked(self, cfg=None) -> Harness:
        h = self.start(cfg)
        h.send({"id": "d", "op": "vf.dump", "args": {"cpus": "0"}})
        self.assertTrue(self.sim.parked.wait(10))
        return h


class ConflictTests(ParkedDump):
    def test_conflicting_requests_get_busy_with_the_running_operation(self) -> None:
        h = self.start_parked()
        reply = h.call("second", "vf.dump", {"cpus": "1"})
        self.assertEqual(error_code(reply), "BUSY")
        self.assertEqual(reply["error"]["detail"], {"state": "planning", "op_id": "d", "op": "vf.dump"})
        self.sim.release.set()
        self.assertTrue(h.response("d")["ok"])
        self.assertEqual(len(self.sim.requests), 10)  # the refused request sent nothing
        self.assertTrue(h.call("third", "vf.dump", {"cpus": "1"})["ok"])  # idle again: no stale BUSY

    def test_a_stub_write_op_is_busy_too_and_never_runs(self) -> None:
        ran: list[str] = []
        stub = OpSpec("stub.write", lambda helper, ctx, args: ran.append(ctx.req_id) or {}, lambda a: a, write=True)
        h = self.start_parked(self.config(extra_ops={"stub.write": stub}))
        self.assertEqual(error_code(h.call("w", "stub.write")), "BUSY")
        self.sim.release.set()
        h.response("d")
        self.assertEqual(ran, [])

    def test_read_only_ops_are_allowed_in_every_state(self) -> None:
        h = self.start_parked()
        self.assertTrue(h.call("p", "preflight")["ok"])
        self.assertTrue(h.call("a", "audit.tail")["ok"])
        self.assertTrue(h.call("t", "telemetry.start", {"interval_s": 0.2})["ok"])
        self.assertTrue(h.call("ts", "telemetry.stop")["ok"])
        self.sim.release.set()
        self.assertTrue(h.response("d")["ok"])


class ReattachTests(ParkedDump):
    def test_repeating_the_running_request_attaches_and_never_runs_it_twice(self) -> None:
        h = self.start_parked()
        for _ in range(3):  # a reload / second tab sends the same request again
            h.send({"id": "d", "op": "vf.dump", "args": {"cpus": "0"}})
        self.assertTrue(h.call("probe", "preflight")["ok"])  # the loop has handled the repeats by now
        self.sim.release.set()
        self.assertTrue(h.response("d")["ok"])
        h.wait_for(lambda m: m.get("event") == "state" and m["state"] == "idle")
        self.assertEqual(len([m for m in h.messages if m.get("id") == "d"]), 1)  # still exactly one final response
        self.assertEqual(len(self.sim.requests), 10)  # and the dump ran once
        self.assertFalse([m for m in h.messages if m.get("ok") is False])  # no BUSY, no cancel, no error for the repeats

    def test_same_id_with_other_args_is_not_a_reattach(self) -> None:
        h = self.start_parked()
        h.send({"id": "d", "op": "vf.dump", "args": {"cpus": "5"}})
        reply = h.wait_for(lambda m: m.get("id") is None and "error" in m)
        self.assertEqual((error_code(reply), reply["error"]["detail"]), ("BAD_REQUEST", {"id": "d"}))
        self.sim.release.set()
        self.assertTrue(h.response("d")["ok"])

    def test_events_of_the_running_operation_carry_its_id(self) -> None:
        h = self.start_parked()
        self.sim.release.set()
        h.response("d")
        for m in h.messages:
            if m.get("event") in ("progress", "state"):
                self.assertEqual(m["op_id"], "d")


class CancelTests(ParkedDump):
    def test_cancel_idle_is_not_an_error(self) -> None:
        h = self.start()
        self.assertEqual(h.call("c", "cancel")["result"], {"cancelled": False, "state": "idle"})

    def test_cancel_takes_effect_between_transactions_only(self) -> None:
        h = self.start_parked()  # parked inside request 2
        ack = h.call("c", "cancel")
        self.assertEqual(ack["result"], {"cancelled": True, "op_id": "d", "state": "planning"})
        self.assertEqual(len(self.sim.requests), 2)
        self.sim.release.set()  # the transaction in flight completes ...
        final = h.response("d")
        self.assertEqual(error_code(final), "CANCELLED")
        self.assertEqual(len(self.sim.requests), 2)  # ... and no third one starts
        self.assertEqual(final["error"]["detail"]["ops_done"], 2)
        self.assertEqual(final["error"]["detail"]["partial"]["units"][0]["cpus"][0]["end"], "stopped")
        h.wait_for(lambda m: m.get("event") == "state" and m["state"] == "idle")
        self.assertEqual(h.call("after", "cancel")["result"]["cancelled"], False)

    def test_cancel_with_the_right_id_and_with_the_wrong_id(self) -> None:
        h = self.start_parked()
        wrong = h.call("c1", "cancel", {"op_id": "not-it"})
        self.assertEqual(error_code(wrong), "BAD_REQUEST")
        self.assertEqual(wrong["error"]["detail"]["op_id"], "d")  # and the operation was not cancelled
        self.assertTrue(h.call("c2", "cancel", {"op_id": "d"})["result"]["cancelled"])
        self.sim.release.set()
        self.assertEqual(error_code(h.response("d")), "CANCELLED")

    def test_a_cancelled_dump_leaves_the_helper_usable(self) -> None:
        h = self.start_parked()
        h.call("c", "cancel")
        self.sim.release.set()
        h.response("d")
        self.sim.park_at = None
        self.assertTrue(h.call("again", "vf.dump", {"cpus": "0"})["ok"])


class ShutdownTests(ParkedDump):
    def test_eof_finishes_the_transaction_stops_and_exits(self) -> None:
        h = self.start_parked()
        h.close_stdin()
        for _ in range(500):  # the stdin reader sets the running op's cancel flag the moment it sees EOF; the transaction is still in flight
            if h.helper._cancel.is_set():
                break
            time.sleep(0.01)
        self.assertTrue(h.helper._cancel.is_set())
        self.sim.release.set()
        final = h.response("d")
        self.assertEqual(error_code(final), "CANCELLED")
        self.assertEqual(len(self.sim.requests), 2)
        self.assertEqual(h.join(), 0)

    def test_shutdown_request_is_the_sigterm_path(self) -> None:
        h = self.start_parked()
        h.helper.request_shutdown("SIGTERM")
        self.sim.release.set()
        self.assertEqual(error_code(h.response("d")), "CANCELLED")
        self.assertEqual(len(self.sim.requests), 2)
        self.assertEqual(h.join(), 0)

    def test_the_signal_handler_flag_is_noticed_by_the_loop(self) -> None:
        h = self.start_parked()
        h.helper._signal = 15  # what the SIGTERM handler does: a plain attribute, no locks in a signal handler
        for _ in range(500):  # the loop polls: wait until it has turned the flag into a shutdown, while the transaction is still in flight
            if h.helper._shutdown.is_set():
                break
            time.sleep(0.01)
        self.assertTrue(h.helper._shutdown.is_set())
        self.sim.release.set()
        self.assertEqual(error_code(h.response("d")), "CANCELLED")
        self.assertEqual(h.join(), 0)

    def test_telemetry_is_stopped_on_exit(self) -> None:
        h = self.start()
        h.call("tel", "telemetry.start", {"interval_s": 0.2})
        h.wait_for(lambda m: m.get("event") == "telemetry")
        h.close_stdin()
        self.assertEqual(h.join(), 0)
        self.assertIsNone(h.helper._tele)

    def test_eof_on_an_idle_helper_exits_cleanly_and_answers_everything_before_it(self) -> None:
        h = self.start()
        h.send({"id": "p", "op": "preflight"})
        h.close_stdin()
        self.assertEqual(h.join(), 0)
        self.assertTrue(h.response("p")["ok"])

    def test_a_vanished_reader_stops_the_helper(self) -> None:
        class Broken(io.RawIOBase):  # stdout whose reader is gone
            def writable(self) -> bool:
                return True

            def write(self, b):  # type: ignore[no-untyped-def]
                raise BrokenPipeError()

        r, w = os.pipe()  # stdin that never ends: only the broken stdout can stop the helper (the heartbeat is the first write)
        stdin = os.fdopen(r, "rb")
        self.addCleanup(stdin.close)  # cleanups run last-in first-out: the writer end closes first, which ends the reader thread's read
        self.addCleanup(os.close, w)
        helper = Helper(self.config())
        done = threading.Event()
        code: list[int] = []
        threading.Thread(target=lambda: (code.append(helper.run(stdin, Broken())), done.set()), daemon=True).start()
        self.assertTrue(done.wait(10))
        self.assertEqual(code, [0])


class RealClockCancelTests(HelperCase):
    """simulator=False runs the real dwell: cancel must wake a dwell wait (the policy default is 60 s)."""

    def test_cancel_wakes_the_dwell_wait(self) -> None:
        import time

        self.policy.write_text(self.policy.read_text().replace("min_dwell_s = 60.0", "min_dwell_s = 30.0"), encoding="utf-8")
        h = self.start(self.config(simulator=False, health_gate_factory=lambda: (lambda phase: (True, "ok"))))
        h.send({"id": "d", "op": "vf.dump", "args": {"cpus": "0"}})
        h.wait_for(lambda m: m.get("event") == "progress" and m["step"] == 1)  # request 1 done, now waiting out the 30 s dwell
        t0 = time.monotonic()
        self.assertTrue(h.call("c", "cancel")["result"]["cancelled"])
        final = h.response("d", timeout=5)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(error_code(final), "CANCELLED")
        self.assertEqual(len(self.sim.requests), 1)

    def test_dwell_refusal_between_sessions_carries_retry_after(self) -> None:
        self.policy.write_text(self.policy.read_text().replace("min_dwell_s = 60.0", "min_dwell_s = 0.2"), encoding="utf-8")
        h = self.start(self.config(simulator=False, health_gate_factory=lambda: (lambda phase: (True, "ok"))))
        self.assertTrue(h.call("a", "vf.dump", {"cpus": "0"})["ok"])
        second = h.call("b", "vf.dump", {"cpus": "0"})  # immediately after: the cross-process dwell is the guard's, the helper does not wait
        self.assertEqual(error_code(second), "REFUSED")
        self.assertEqual(second["error"]["detail"]["check"], "DWELL")
        self.assertGreater(second["error"]["detail"]["retry_after_s"], 0)


class InstanceLockTests(HelperCase):
    def test_a_second_helper_on_the_same_lock_refuses_to_start(self) -> None:
        first = self.start()
        self.assertTrue(first.call("p", "preflight")["ok"])
        second = Harness(self.config())
        self.harnesses.append(second)
        second.start()
        msg = second.response(None)
        self.assertEqual(error_code(msg), "REFUSED")
        self.assertEqual(msg["error"]["detail"]["check"], "INSTANCE_LOCK")
        self.assertEqual(second.join(), EXIT_INSTANCE_BUSY)
        self.assertEqual(second.messages, [msg])  # one line, nothing else
        self.assertTrue(first.call("p2", "preflight")["ok"])  # the first one is unaffected

    def test_the_lock_is_free_again_after_the_first_exits(self) -> None:
        first = self.start()
        first.close_stdin()
        first.join()
        again = self.start()
        self.assertTrue(again.call("p", "preflight")["ok"])

    def test_the_instance_lock_does_not_use_the_guards_lock_file(self) -> None:
        lock = InstanceLock(self.lock)
        self.assertNotEqual(lock.path, str(self.lock))
        lock.acquire()
        try:
            self.assertFalse(self.lock.exists())  # the guard's own O_EXCL file is untouched, so the guard can still take it per transaction
            self.assertTrue(os.path.exists(lock.path))
        finally:
            lock.release()

    def test_guard_lock_contention_is_the_guards_refusal(self) -> None:
        # the CLI (or anything else) inside a transaction holds the guard's lock file: the dump is refused LOCKED, not queued
        self.lock.write_text("1234\n", encoding="utf-8")
        reply = self.start().call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual(reply["error"]["detail"]["check"], "LOCKED")


class HelperObjectTests(HelperCase):
    def test_extra_ops_are_registered_next_to_the_builtin_ones(self) -> None:
        stub = OpSpec("stub.read", lambda helper, ctx, args: {"echo": args}, lambda a: a)
        h = Helper(self.config(extra_ops={"stub.read": stub}))
        self.assertIn("stub.read", h.ops)
        self.assertEqual(set(h.ops) & {"preflight", "vf.dump", "audit.tail", "telemetry.start", "telemetry.stop", "cancel"}, {"preflight", "vf.dump", "audit.tail", "telemetry.start", "telemetry.stop", "cancel"})

    def test_the_g1_ops_and_the_g111_ops_exist(self) -> None:
        g1 = {"preflight", "vf.dump", "audit.tail", "telemetry.start", "telemetry.stop", "cancel"}
        g111 = {"vf.measure", "curves.list", "curves.get", "curves.update", "curves.delete", "mem.read"}
        self.assertEqual(set(Helper(self.config()).ops) & (g1 | g111), g1 | g111)


if __name__ == "__main__":
    unittest.main()
