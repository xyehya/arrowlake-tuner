"""The helper's load generator and the wiring factories: how the spinners are launched (no shell, fixed env, pinned, self-deadline, trusted interpreter)."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from arltuner.helper import main as helper_main
from arltuner.helper.ops_measure import HelperMeasureIO, SimMeasureIO, SimModel, sim_wiring
from arltuner.helper.trust import TrustError


class FakeProc:
    def __init__(self, args: list[str], kwargs: dict[str, Any]) -> None:
        self.args, self.kwargs = args, kwargs
        self.terminated = False
        self.killed = False
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode if self.returncode is not None else 0


class SpawnTests(unittest.TestCase):
    def make(self, *, fail_at: int | None = None, **kw: Any) -> tuple[HelperMeasureIO, list[FakeProc]]:
        procs: list[FakeProc] = []

        def popen(args: list[str], **kwargs: Any) -> FakeProc:
            if fail_at is not None and len(procs) == fail_at:
                raise OSError("no more processes")
            procs.append(FakeProc(args, kwargs))
            return procs[-1]

        return HelperMeasureIO(Path("/nonexistent"), popen=popen, python=sys.executable, **kw), procs

    def test_one_pinned_isolated_busy_loop_per_cpu_with_a_deadline(self) -> None:
        io, procs = self.make()
        load = io.start_load([12, 13, 14, 15], 4.2)
        self.assertEqual(len(procs), 4)
        exe = os.path.realpath(sys.executable)
        for cpu, p in zip((12, 13, 14, 15), procs):
            self.assertEqual(p.args[:3], [exe, "-I", "-c"])
            self.assertIn(f"os.sched_setaffinity(0,{{{cpu}}})", p.args[3])
            self.assertIn("t=time.monotonic()+4.2", p.args[3])  # its own deadline: a dead helper cannot orphan it
            compile(p.args[3], "<spinner>", "exec")
            self.assertEqual(p.kwargs["cwd"], "/")
            self.assertEqual(p.kwargs["env"], {"LC_ALL": "C"})
            self.assertIs(p.kwargs["shell"], False)
            self.assertIs(p.kwargs["close_fds"], True)
            self.assertEqual((p.kwargs["stdin"], p.kwargs["stdout"], p.kwargs["stderr"]), (subprocess.DEVNULL,) * 3)
        load.stop()
        self.assertTrue(all(p.terminated for p in procs))

    def test_a_failed_spawn_stops_the_ones_already_running(self) -> None:
        io, procs = self.make(fail_at=2)
        with self.assertRaises(OSError):
            io.start_load([8, 9, 10, 11], 3)
        self.assertEqual(len(procs), 2)
        self.assertTrue(all(p.terminated for p in procs))

    def test_stop_kills_what_will_not_terminate(self) -> None:
        io, procs = self.make()
        load = io.start_load([0], 3)
        procs[0].terminate = lambda: None  # type: ignore[method-assign]
        calls = {"n": 0}

        def wait(timeout: float | None = None) -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                raise subprocess.TimeoutExpired("x", 5)
            return 0

        procs[0].wait = wait  # type: ignore[method-assign]
        load.stop()
        self.assertTrue(procs[0].killed)

    def test_the_interpreter_is_resolved_and_trust_checked_before_every_spawn(self) -> None:
        seen: list[str] = []
        link = Path(tempfile.mkdtemp()) / "python3"
        link.symlink_to(sys.executable)
        self.addCleanup(link.unlink)
        io, procs = self.make(trust_check=seen.append)
        io._python = str(link)
        io.start_load([0], 2)
        io.start_load([1], 2)
        self.assertEqual(seen, [os.path.realpath(sys.executable)] * 2)  # the symlink (Fedora's /usr/bin/python3) is resolved first, and the target is what runs
        self.assertEqual(procs[0].args[0], os.path.realpath(sys.executable))

    def test_an_untrusted_interpreter_is_refused(self) -> None:
        def refuse(path: str) -> None:
            raise TrustError(path, "owner uid 1000 is not root")

        io, procs = self.make(trust_check=refuse)
        self.assertIn("not usable", io.check() or "")
        with self.assertRaises(TrustError):
            io.start_load([0], 2)
        self.assertEqual(procs, [])

    @unittest.skipUnless(hasattr(os, "sched_getaffinity"), "Linux only")
    def test_the_real_spinner_runs_pinned_and_stops(self) -> None:
        cpu = sorted(os.sched_getaffinity(0))[0]
        io = HelperMeasureIO(Path("/nonexistent"), python=sys.executable)
        load = io.start_load([cpu], 3)
        proc = load._procs[0]
        time.sleep(0.4)
        self.assertIsNone(proc.poll(), "the spinner is still busy")
        load.stop()
        self.assertIsNotNone(proc.poll())


class SimIoTests(unittest.TestCase):
    def test_the_model_settles_where_the_search_expects(self) -> None:
        m = SimModel()
        self.assertEqual(m.settled("P0", 5_700_000), 57)
        self.assertEqual(m.settled("P1", 5_700_000), 55)  # 56x / 57x unreachable except P0 / P3
        self.assertEqual(m.settled("EC0", 2_300_000), 22)  # the E quirk: cap = ratio gives one bin low ...
        self.assertEqual(m.settled("EC0", 2_400_000), 24)  # F9: ... but for 23x the +1 cap lands on 24: 23x is not pinnable
        self.assertEqual(m.settled("EC0", 2_500_000), 24)  # (cap 25 -> 24, the quirk)
        self.assertEqual(m.settled("P4", 5_200_000), 51)  # F9: P 52x is not pinnable either
        self.assertEqual(m.settled("P4", 5_300_000), 53)
        self.assertEqual(m.settled("EC0", 3_000_000), 30)
        self.assertEqual(m.settled("EC0", 4_600_000), 46)
        self.assertEqual(m.settled("EC0", 4_800_000), 48)  # the E turbo table (0x650: 48x)
        self.assertEqual((m.max_khz(0), m.max_khz(1), m.max_khz(8)), (5_700_000, 5_500_000, 4_900_000))  # E cpuinfo_max_freq 4.9 GHz although the table stops at 48x

    def test_the_sim_wiring_lives_under_the_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            w = sim_wiring(Path(tmp) / "state")
            self.assertEqual(w.store.root, Path(tmp) / "state" / "curves")
            self.assertEqual(w.journal_path.parent, Path(tmp) / "state")
            self.assertIsInstance(w.make_run().io, SimMeasureIO)
            self.assertEqual(w.mem_fn()["mts"], 7200)


class MainWiringTests(unittest.TestCase):
    def test_sim_uses_the_given_state_dir_or_a_private_temp_one_never_var_lib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            w = helper_main.measure_wiring(argparse.Namespace(sim=True, state_dir=str(Path(tmp) / "s")))
            self.assertTrue(str(w.store.root).startswith(tmp))
        w = helper_main.measure_wiring(argparse.Namespace(sim=True, state_dir=None))
        scratch = w.journal_path.parent
        self.addCleanup(lambda: __import__("shutil").rmtree(scratch, ignore_errors=True))
        self.assertFalse(str(w.store.root).startswith("/var/lib"))
        self.assertTrue(str(scratch).startswith(tempfile.gettempdir()))
        self.assertFalse(scratch.exists(), "nothing is created until a run needs it")
        w.prepare()
        self.assertEqual(oct(os.stat(scratch).st_mode & 0o777), "0o700")

    def test_the_state_dir_option_exists_for_sim_and_is_refused_as_root(self) -> None:
        ns = helper_main._parser().parse_args(["--sim", "--state-dir", "/tmp/x"])
        self.assertEqual(ns.state_dir, "/tmp/x")
        refusal = helper_main.root_argv_refusal(["--device", "--state-dir=/tmp/x"])
        self.assertEqual(refusal[0], "ROOT_ARGV")

    def test_the_device_wiring_uses_installed_paths_only(self) -> None:
        w = helper_main.measure_wiring(argparse.Namespace(sim=False, state_dir="/ignored"))
        self.assertEqual(str(w.store.root), "/var/lib/arltuner/curves")
        self.assertEqual(str(w.journal_path), "/run/arltuner/measure-journal.json")
        self.assertTrue(w.store.enforce_trust)
        self.assertIsInstance(w.make_run, type(lambda: 0))  # built lazily: nothing was opened


if __name__ == "__main__":
    unittest.main()
