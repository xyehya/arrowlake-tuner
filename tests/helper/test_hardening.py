"""Fix round: argv as the trust boundary (items 1/2), hard links (3), bounded memory (5), a dispatch loop that cannot be stalled (6), telemetry
descriptors (7), cancel vs the transport sleep (9), thread-start failure (10). Simulator, fake stat results and injected euid only."""
from __future__ import annotations

import argparse
import io
import json
import os
import stat
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from arltuner.cli import default_lock_path
from arltuner.helper import OpSpec, core, ops
from arltuner.helper import main as helper_main
from arltuner.helper.trust import TrustError, check_lock_file, open_checked

from ._support import GOOD_REPORT, Harness, HelperCase, error_code

POSIX = os.name == "posix"
DIR = stat.S_IFDIR | 0o755
FILE = stat.S_IFREG | 0o644


def ns(**kw) -> argparse.Namespace:
    base = dict(sim=False, device=True, policy=None, audit=None, lock=None, allow_taint_bit=[])
    base.update(kw)
    return argparse.Namespace(**base)


class RootArgvTests(unittest.TestCase):
    """Item 1: when the euid is 0 argv is untrusted input; the helper accepts only --device, or --device --socket (D-029, D-043)."""

    def run_main(self, argv, euid=0):
        out, err = io.BytesIO(), io.StringIO()
        code = helper_main.main(argv, stdin=io.BytesIO(), stdout=out, stderr=err, euid_fn=lambda: euid)
        return code, [json.loads(line) for line in out.getvalue().splitlines()], err.getvalue()

    def test_sim_is_refused_outright_as_root(self) -> None:
        for argv in (["--sim"], ["--sim", "--audit", "/tmp/x"], ["--sim=1"]):
            with self.subTest(argv=argv):
                code, lines, _ = self.run_main(argv)
                self.assertEqual(code, 77)
                (msg,) = lines
                self.assertEqual((msg["ok"], msg["error"]["code"], msg["error"]["detail"]["check"]), (False, "REFUSED", "ROOT_SIM"))

    def test_no_path_or_safety_argv_is_accepted_as_root(self) -> None:
        for argv in (["--audit", "/tmp/a"], ["--audit=/tmp/a"], ["--lock", "/tmp/l"], ["--policy", "/tmp/p"], ["--allow-taint-bit", "34"],
                     ["--allow-taint-bit=34"], ["--aud", "/tmp/a"], ["--device", "--policy", "/tmp/p"], ["stray"], ["-h"]):
            with self.subTest(argv=argv):
                code, lines, err = self.run_main(argv)
                self.assertEqual(code, 77, (argv, err))
                (msg,) = lines  # one refusal line; the helper never started (no instance lock, no protocol)
                self.assertEqual((msg["error"]["code"], msg["error"]["detail"]["check"]), ("REFUSED", "ROOT_ARGV"))

    def test_root_argv_refusal_function(self) -> None:
        self.assertIsNone(helper_main.root_argv_refusal([]))
        self.assertIsNone(helper_main.root_argv_refusal(["--device"]))
        self.assertEqual(helper_main.root_argv_refusal(["--sim"])[0], "ROOT_SIM")
        self.assertIsNone(helper_main.root_argv_refusal(["--device", "--socket"]))
        self.assertEqual(helper_main.root_argv_refusal(["--device", "--lock=/x"])[0], "ROOT_ARGV")

    def test_socket_argv_is_exactly_device_then_socket(self) -> None:
        for argv in (["--socket"], ["--socket", "--device"], ["--device", "--device"], ["--device", "--socket", "--socket"], ["--device", "--socket", "x"],
                     ["--socket-path", "/tmp/s"], ["--device", "--socket", "--socket-path=/tmp/s"], ["--device", "--socket", "--helper-toml", "/tmp/h"],
                     ["--socket=1"], ["--device", "--socket", "--sim"]):
            with self.subTest(argv=argv):
                code, lines, _ = self.run_main(argv)
                self.assertEqual(code, 77)
                self.assertIn(lines[0]["error"]["detail"]["check"], ("ROOT_ARGV", "ROOT_SIM"))

    def test_root_socket_mode_builds_the_same_installed_config(self) -> None:
        cfg = helper_main.build_config(ns(socket=True, audit="/tmp/evil"), log=lambda t: None, root=True)
        self.assertEqual((cfg.audit_path, cfg.policy_path, cfg.operator), (helper_main.INSTALLED_AUDIT, helper_main.INSTALLED_POLICY, "helper"))

    def test_root_with_no_argv_still_has_to_start_from_a_trusted_code_tree(self) -> None:
        code, lines, _ = self.run_main([])  # a checkout is not root-owned: STARTUP refusal, nothing else happens
        if os.geteuid() == 0:
            self.skipTest("running as real root")
        self.assertEqual(code, 77)
        self.assertEqual(lines[0]["error"]["detail"]["check"], "STARTUP")

    def test_non_root_keeps_its_options(self) -> None:
        code, lines, err = self.run_main([], euid=1000)
        self.assertEqual((code, lines), (64, []))  # usage: needs --sim or --device
        self.assertIn("required", err)

    def test_root_config_comes_from_installed_constants_whatever_the_namespace_says(self) -> None:
        hostile = ns(audit="/tmp/evil-audit", policy="/tmp/evil-policy", lock="/tmp/evil-lock", allow_taint_bit=[1, 2, 3])
        cfg = helper_main.build_config(hostile, log=lambda t: None, root=True)
        self.assertEqual(cfg.audit_path, helper_main.INSTALLED_AUDIT)
        self.assertEqual(cfg.policy_path, helper_main.INSTALLED_POLICY)
        self.assertEqual(cfg.lock_path, default_lock_path())
        self.assertEqual((cfg.audit_path, cfg.policy_path), (Path("/var/lib/arltuner/audit.jsonl"), Path("/etc/arltuner/policy.toml")))
        self.assertTrue(cfg.enforce_trust)
        self.assertFalse(cfg.simulator)
        self.assertEqual(helper_main.INSTALLED_ACCEPTED_DIR, Path("/etc/arltuner/accepted"))

    def test_root_never_builds_the_simulator(self) -> None:
        with self.assertRaises(Exception):
            helper_main.build_config(ns(sim=True, device=False), log=lambda t: None, root=True)

    def test_non_root_device_options_are_used_as_given(self) -> None:
        cfg = helper_main.build_config(ns(audit="/a", policy="/p", lock="/l"), log=lambda t: None, root=False)
        self.assertEqual((cfg.audit_path, cfg.policy_path, cfg.lock_path), (Path("/a"), Path("/p"), Path("/l")))


def tree_for(path: Path, uid: int) -> dict[str, tuple[int, int]]:
    tree = {"/": (uid, DIR)}
    for parent in reversed(path.parents):
        tree[str(parent)] = (uid, DIR)
    tree[str(path)] = (uid, FILE)
    return tree


def fake_lstat(tree):
    def lstat(p: str):
        try:
            uid, mode = tree[p]
        except KeyError:
            raise FileNotFoundError(p) from None
        return types.SimpleNamespace(st_uid=uid, st_mode=mode, st_nlink=1)

    return lstat


class TaintBitsConfigTests(HelperCase):
    """Item 2: the expected first-write taint bits are a key of the root-owned /etc/arltuner/helper.toml, never argv."""

    def load(self, text: str | None, **tree_changes):
        path = self.tmp / "helper.toml"
        uid = os.geteuid()
        if text is not None:
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
        tree = tree_for(path, uid) | tree_changes
        return helper_main.load_taint_bits(path, lstat_fn=fake_lstat(tree), root_uid=uid)

    def test_absent_file_means_none(self) -> None:
        self.assertEqual(self.load(None, **{}), frozenset())

    def test_the_key_is_read(self) -> None:
        self.assertEqual(self.load("allow_taint_bits = [34, 12]\n"), frozenset({34, 12}))
        self.assertEqual(self.load("allow_taint_bits = []\n"), frozenset())

    def test_the_access_table_and_idle_exit_do_not_disturb_the_taint_bits(self) -> None:
        self.assertEqual(self.load('allow_taint_bits = [2]\nidle_exit_s = 60\n[access]\nusers = ["a"]\ngroup = "arltuner"\n'), frozenset({2}))
        with self.assertRaises(ValueError):  # a malformed [access] makes the whole file invalid (fail closed)
            self.load('allow_taint_bits = [2]\n[access]\nusers = "a"\n')

    def test_unknown_keys_and_bad_values_are_refused(self) -> None:
        for text in ("other = 1\n", "allow_taint_bits = [64]\n", "allow_taint_bits = [-1]\n", 'allow_taint_bits = ["34"]\n', "allow_taint_bits = 34\n",
                     "allow_taint_bits = [true]\n", "not toml [\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load(text)

    def test_an_untrusted_file_or_directory_is_refused(self) -> None:
        path = self.tmp / "helper.toml"
        with self.assertRaises(TrustError):
            self.load("allow_taint_bits = [34]\n", **{str(path): (os.geteuid(), stat.S_IFREG | 0o666)})
        with self.assertRaises(TrustError):
            self.load("allow_taint_bits = [34]\n", **{str(self.tmp): (os.geteuid() + 1, DIR)})

    def test_vf_dump_reports_a_bad_config_or_untrusted_file_by_name(self) -> None:
        def untrusted():
            raise TrustError("/etc/arltuner/helper.toml", "owner uid 1000 is not root")

        def invalid():
            raise ValueError("allow_taint_bits must be a list")

        for factory, check in ((untrusted, "TRUST_PATH"), (invalid, "HELPER_CONFIG_INVALID")):
            with self.subTest(check=check):
                h = self.start(self.config(health_gate_factory=factory))
                reply = h.call("d", "vf.dump", {"cpus": "0"})
                self.assertEqual((error_code(reply), reply["error"]["detail"]["check"]), ("REFUSED", check))
                self.assertEqual(self.sim.wrmsr_count, 0)
                h.stop()


@unittest.skipUnless(POSIX, "hard links")
class HardLinkTests(HelperCase):
    """Item 3: an alias of some other file (st_nlink != 1) is never opened for writing; O_TRUNC is never used."""

    def target(self) -> Path:
        t = self.tmp / "target"
        t.write_text("precious\n")
        return t

    def test_open_checked_refuses_a_hard_link_and_never_truncates(self) -> None:
        target = self.target()
        alias = self.tmp / "alias"
        os.link(target, alias)
        for create in (True, False):
            with self.subTest(create=create), self.assertRaises(TrustError) as ctx:
                open_checked(alias, write=True, create=create, euid=os.geteuid())
            self.assertIn("hard links", ctx.exception.reason)
        self.assertEqual(target.read_text(), "precious\n")

    def test_an_existing_file_is_not_truncated_by_the_open(self) -> None:
        p = self.tmp / "keep"
        p.write_text("data\n")
        os.close(open_checked(p, write=True, create=True, euid=os.geteuid()))
        self.assertEqual(p.read_text(), "data\n")

    def test_hard_link_at_the_audit_path(self) -> None:
        target = self.target()
        os.link(target, self.audit)
        h = self.start()
        for rid, op, args in (("d", "vf.dump", {"cpus": "0"}), ("t", "audit.tail", {})):
            reply = h.call(rid, op, args)
            self.assertEqual((error_code(reply), reply["error"]["detail"]["check"]), ("REFUSED", "TRUST_PATH"))
            self.assertIn("hard links", reply["error"]["detail"]["reason"])
        self.assertEqual(target.read_text(), "precious\n")
        self.assertEqual(self.sim.wrmsr_count, 0)

    def test_hard_link_at_the_guard_lock_path(self) -> None:
        target = self.target()
        os.link(target, self.lock)
        reply = self.start().call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual((error_code(reply), reply["error"]["detail"]["check"]), ("REFUSED", "LOCK_UNTRUSTED"))
        self.assertEqual(target.read_text(), "precious\n")
        self.assertEqual(self.sim.wrmsr_count, 0)
        with self.assertRaises(TrustError):
            check_lock_file(self.lock, euid=os.geteuid())

    def test_hard_link_at_the_instance_lock_path_refuses_to_start(self) -> None:
        target = self.target()
        os.link(target, self.lock.with_name(self.lock.name + ".helper"))
        h = Harness(self.config())
        self.harnesses.append(h)
        h.start()
        msg = h.response(None)
        self.assertEqual((error_code(msg), msg["error"]["detail"]["check"]), ("REFUSED", "LOCK_UNTRUSTED"))
        self.assertEqual(h.join(), 77)
        self.assertEqual(target.read_text(), "precious\n")

    def test_fifo_at_the_instance_and_guard_lock_paths_is_refused_without_a_hang(self) -> None:
        os.mkfifo(self.lock)
        reply = self.start().call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual(reply["error"]["detail"]["check"], "LOCK_UNTRUSTED")
        os.mkfifo(self.lock.with_name("fifo.lock.helper"))
        h = Harness(self.config(lock_path=self.lock.with_name("fifo.lock")))
        self.harnesses.append(h)
        h.start()
        self.assertEqual(h.response(None)["error"]["detail"]["check"], "LOCK_UNTRUSTED")
        self.assertEqual(h.join(), 77)


class BlockingOps:
    """Stub ops that park the dispatch loop (an inline op) or a worker, to prove what can and cannot stall it."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.block_inline = OpSpec("stub.block", self._block, lambda a: a)

    def _block(self, helper, ctx, args):
        self.entered.set()
        self.release.wait(10)
        return {"released": True}

    def extra(self):
        return {"stub.block": self.block_inline}


class BoundedMemoryTests(HelperCase):
    """Item 5: the id set is an LRU and the request queue is bounded; overflow is refused (BUSY with detail.queue = 'full'), never buffered."""

    def test_seen_ids_are_capped_and_the_oldest_may_be_reused(self) -> None:
        with mock.patch.object(core, "SEEN_MAX", 8):
            h = self.start()
            for i in range(30):
                self.assertTrue(h.call(f"r{i}", "telemetry.stop")["ok"])
            self.assertLessEqual(len(h.helper._seen), 8)
            self.assertTrue(h.call("r0", "telemetry.stop")["ok"])  # forgotten: accepted again (documented window)
            h.send({"id": "r29", "op": "telemetry.stop"})  # recent: still refused
            self.assertEqual(error_code(h.wait_for(lambda m: m.get("id") is None and "error" in m)), "BAD_REQUEST")

    def test_an_in_flight_id_is_never_evicted(self) -> None:
        from ._support import BlockingSim

        self.sim = sim = BlockingSim(self.sim.config)
        sim.park_at = 1
        with mock.patch.object(core, "SEEN_MAX", 4):
            h = self.start()
            h.send({"id": "d", "op": "vf.dump", "args": {"cpus": "0"}})
            self.assertTrue(sim.parked.wait(10))
            for i in range(12):
                h.call(f"x{i}", "telemetry.stop")
            self.assertIn("d", h.helper._seen)
            sim.release.set()
            self.assertTrue(h.response("d")["ok"])

    def test_the_request_queue_is_bounded_every_request_is_answered_once_and_eof_still_gets_in(self) -> None:
        blocker = BlockingOps()
        with mock.patch.object(core, "QUEUE_MAX", 4):
            h = self.start(self.config(extra_ops=blocker.extra()))
            h.send({"id": "b", "op": "stub.block"})
            self.assertTrue(blocker.entered.wait(10))  # the dispatch loop is now stuck inside an inline op
            for i in range(30):
                h.send({"id": f"q{i}", "op": "telemetry.stop"})
            # Dispatch is parked inside "b", so the outcome is fixed: q0..q3 fill the queue and q4..q29 are refused. The earlier version released
            # the blocker after the FIRST BUSY, while the reader was still draining the flood; room appeared mid-flood and the count varied.
            flood, queue_max = 30, 4
            self.wait_until(h, lambda msgs: sum(1 for m in msgs if self.is_busy(m)) == flood - queue_max)
            busy = next(m for m in h.messages if self.is_busy(m))
            self.assertEqual(busy["error"]["detail"]["queue"], "full")
            self.assertEqual(busy["error"]["detail"]["state"], "idle")
            h.close_stdin()  # EOF has to wait for room, not be dropped
            blocker.release.set()
            self.assertEqual(h.join(), 0)
        answers: dict[str, list[dict]] = {}
        for m in h.messages:
            if m.get("id") is not None:
                answers.setdefault(m["id"], []).append(m)
        self.assertEqual(set(answers), {"b"} | {f"q{i}" for i in range(flood)})
        self.assertTrue(all(len(v) == 1 for v in answers.values()))  # never two answers
        self.assertTrue(answers["b"][0]["ok"])
        for i in range(queue_max):  # the queued ones were served after the release
            self.assertTrue(answers[f"q{i}"][0]["ok"], answers[f"q{i}"])
        for i in range(queue_max, flood):  # the rest were refused, each under its own id
            self.assertTrue(self.is_busy(answers[f"q{i}"][0]), answers[f"q{i}"])

    @staticmethod
    def is_busy(m: dict) -> bool:
        return m.get("ok") is False and m["error"]["code"] == "BUSY"

    @staticmethod
    def wait_until(h, pred, timeout: float = 10.0) -> None:
        import time

        deadline = time.monotonic() + timeout
        with h._cond:
            while not pred(list(h.messages)):
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"timed out; saw {len(h.messages)} messages")
                h._cond.wait(left)


class StallTests(HelperCase):
    """Item 6: preflight and audit.tail run on their own threads: cancel, EOF and signals stay responsive while they block."""

    def hanging_preflight(self):
        gate = threading.Event()
        started = threading.Semaphore(0)

        def preflight_fn():
            started.release()
            gate.wait(20)
            return GOOD_REPORT

        self.addCleanup(gate.set)
        return gate, started, preflight_fn

    def test_cancel_still_answers_while_a_preflight_is_wedged(self) -> None:
        gate, started, fn = self.hanging_preflight()
        h = self.start(self.config(preflight_fn=fn))
        h.send({"id": "p", "op": "preflight"})
        self.assertTrue(started.acquire(timeout=10))
        self.assertEqual(h.call("c", "cancel", timeout=3)["result"]["cancelled"], False)  # answered at once
        self.assertTrue(h.call("t", "telemetry.stop", timeout=3)["ok"])
        gate.set()
        self.assertTrue(h.response("p")["ok"])

    def test_shutdown_does_not_wait_for_a_wedged_preflight_for_long(self) -> None:
        import time

        gate, started, fn = self.hanging_preflight()
        with mock.patch.object(core, "_SLOW_JOIN_S", 0.3):
            h = self.start(self.config(preflight_fn=fn))
            h.send({"id": "p", "op": "preflight"})
            self.assertTrue(started.acquire(timeout=10))
            t0 = time.monotonic()
            h.close_stdin()  # EOF while the preflight hangs
            self.assertEqual(h.join(5), 0)
            self.assertLess(time.monotonic() - t0, 4)
        gate.set()

    def test_sigterm_flag_is_noticed_while_a_preflight_is_wedged(self) -> None:
        gate, started, fn = self.hanging_preflight()
        with mock.patch.object(core, "_SLOW_JOIN_S", 0.3):
            h = self.start(self.config(preflight_fn=fn))
            h.send({"id": "p", "op": "preflight"})
            self.assertTrue(started.acquire(timeout=10))
            h.helper._signal = 15
            self.assertEqual(h.join(5), 0)
        gate.set()

    def test_only_a_few_read_only_ops_run_at_once(self) -> None:
        gate, started, fn = self.hanging_preflight()
        h = self.start(self.config(preflight_fn=fn))
        for i in range(core.SLOW_MAX):
            h.send({"id": f"p{i}", "op": "preflight"})
            self.assertTrue(started.acquire(timeout=10))
        reply = h.call("extra", "audit.tail", timeout=3)
        self.assertEqual((error_code(reply), reply["error"]["detail"]["slow_ops"]), ("BUSY", core.SLOW_MAX))
        gate.set()
        for i in range(core.SLOW_MAX):
            self.assertTrue(h.response(f"p{i}")["ok"])
        self.assertTrue(h.call("again", "audit.tail")["ok"])  # the slots are free again

    def test_repeating_a_running_preflight_attaches_instead_of_starting_another(self) -> None:
        gate, started, fn = self.hanging_preflight()
        h = self.start(self.config(preflight_fn=fn))
        h.send({"id": "p", "op": "preflight"})
        self.assertTrue(started.acquire(timeout=10))
        h.send({"id": "p", "op": "preflight"})
        self.assertTrue(h.call("probe", "cancel")["ok"])
        self.assertFalse(started.acquire(timeout=0.3))  # no second run
        gate.set()
        self.assertTrue(h.response("p")["ok"])
        self.assertEqual(len([m for m in h.messages if m.get("id") == "p"]), 1)

    def test_audit_tail_reads_at_most_the_last_256_kib(self) -> None:
        self.assertEqual(ops._TAIL_READ_BYTES, 256 * 1024)
        self.audit.write_text("".join(json.dumps({"seq": i, "pad": "x" * 80}) + "\n" for i in range(100)), encoding="utf-8")
        with mock.patch.object(ops, "_TAIL_READ_BYTES", 2048):
            result = self.start().call("t", "audit.tail", {"n": 200})["result"]
        self.assertTrue(result["truncated"])
        self.assertLess(len(result["records"]), 25)  # ~2 KiB of ~100-byte lines
        self.assertEqual(result["records"][-1]["seq"], 99)
        self.assertTrue(all("seq" in r for r in result["records"]))  # the cut first line was dropped, not parsed as garbage


class TelemetryDescriptorTests(HelperCase):
    """Item 7: nothing keeps the telemetry MSR port open after a failed start or a stop."""

    def test_a_failed_start_stops_the_sampler(self) -> None:
        calls: list[str] = []

        class Sampler:
            def start(self) -> None:
                calls.append("start")
                raise RuntimeError("boom")

            def stop(self) -> None:
                calls.append("stop")

        reply = self.start(self.config(sampler_factory=lambda interval: Sampler())).call("t", "telemetry.start")
        self.assertEqual(error_code(reply), "INTERNAL")
        self.assertEqual(calls, ["start", "stop"])

    def test_port_owning_sampler_closes_the_port_on_failed_start_and_on_stop(self) -> None:
        closed: list[int] = []
        port = types.SimpleNamespace(close=lambda: closed.append(1))

        class Failing:
            def start(self):
                raise RuntimeError("boom")

            def stop(self):
                pass

        failing = helper_main.PortOwningSampler(port, Failing())
        with self.assertRaises(RuntimeError):
            failing.start()
        self.assertEqual(closed, [1])
        ok = helper_main.PortOwningSampler(port, types.SimpleNamespace(start=lambda: None, stop=lambda: None, snapshot=lambda: {"ts": 1}))
        ok.start()
        self.assertEqual(ok.snapshot(), {"ts": 1})
        ok.stop()
        self.assertEqual(closed, [1, 1])

    def test_the_hardware_factory_closes_its_port_if_the_sampler_cannot_be_built(self) -> None:
        closed: list[int] = []
        port = types.SimpleNamespace(close=lambda: closed.append(1))
        with mock.patch("arltuner.msr.DevCpuMsrPort", return_value=port), mock.patch("arltuner.telemetry.Sampler", side_effect=RuntimeError("no sampler")):
            cfg = helper_main.build_config(ns(audit="/a", policy="/p"), log=lambda t: None)  # imports the (patched) names at build time
            with self.assertRaises(RuntimeError):
                cfg.sampler_factory(1.0)
        self.assertEqual(closed, [1])


class CancelVersusTransportSleepTests(HelperCase):
    """Item 9: cancel is honoured between transactions only; a set cancel flag must not make the transport's poll sleep return at once (a spin)."""

    def test_transport_gets_the_real_sleep_not_the_cancel_aware_one(self) -> None:
        from ._support import BlockingSim

        self.sim = sim = BlockingSim(self.sim.config)
        sim.park_at = 1
        real_sleeps: list[float] = []
        captured: list[object] = []
        real_transport = ops.MailboxTransport

        def spy(port, **kw):
            captured.append(kw["sleep"])
            return real_transport(port, **kw)

        cfg = self.config(simulator=False, health_gate_factory=lambda: (lambda phase: (True, "ok")), sleep=real_sleeps.append)
        with mock.patch.object(ops, "MailboxTransport", spy):
            h = self.start(cfg)
            h.send({"id": "d", "op": "vf.dump", "args": {"cpus": "0"}})
            self.assertTrue(sim.parked.wait(10))
            self.assertTrue(h.call("c", "cancel")["result"]["cancelled"])  # the cancel flag is set while a transaction is in flight
            transport_sleep = captured[0]
            transport_sleep(0.01)
            sim.release.set()
            h.response("d")
        self.assertIs(captured[0], cfg.sleep)  # the transport's sleep is cfg.sleep: unaffected by the cancel flag (no spin)
        self.assertIn(0.01, real_sleeps)


class ThreadStartFailureTests(HelperCase):
    """Item 10: if the worker thread cannot start, the machine goes back to idle and the request gets an INTERNAL response."""

    def test_machine_returns_to_idle_and_a_later_dump_works(self) -> None:
        real = threading.Thread

        class Refusing(real):
            fail = True

            def start(self):
                if Refusing.fail and self.name == "helper-op-vf.dump":
                    raise RuntimeError("can't start new thread")
                super().start()

        with mock.patch.object(core.threading, "Thread", Refusing):
            h = self.start()
            reply = h.call("d", "vf.dump", {"cpus": "0"})
            self.assertEqual(error_code(reply), "INTERNAL")
            self.assertIn("can't start new thread", reply["error"]["message"])
            h.wait_for(lambda m: m.get("event") == "state" and m["state"] == "idle")
            self.assertEqual(h.helper.machine.state.value, "idle")
            self.assertEqual(h.helper._inflight, {})
            self.assertEqual(h.call("c", "cancel")["result"]["state"], "idle")
            Refusing.fail = False
            self.assertTrue(h.call("again", "vf.dump", {"cpus": "0"})["ok"])  # no stale BUSY
        self.assertEqual(self.sim.wrmsr_count > 0, True)

    def test_a_slow_op_thread_that_cannot_start_is_an_internal_response(self) -> None:
        real = threading.Thread

        class Refusing(real):
            def start(self):
                if self.name == "helper-op-preflight":
                    raise RuntimeError("no threads")
                super().start()

        with mock.patch.object(core.threading, "Thread", Refusing):
            h = self.start()
            self.assertEqual(error_code(h.call("p", "preflight")), "INTERNAL")
            self.assertEqual(h.helper._inflight, {})
            self.assertEqual(len(h.helper._slow), 0)


if __name__ == "__main__":
    unittest.main()
