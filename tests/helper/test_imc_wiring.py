"""G-109: the device sampler owns an IMC (DRAM bandwidth) source with the hardened perf spawn; --sim has none. Fake popen, fake lstat, fake MSR port."""
from __future__ import annotations

import io
import stat
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from arltuner.helper import main as helper_main
from arltuner.telemetry import ImcStat, Sampler, TelemetryMsr

from tests.telemetry._support import FakeMsrPort

from .test_hardening import ns

ROW = "1.0,1024.00,MiB,uncore_imc_free_running/data_{}/,1000000000,100.00,,\n"


class FakeProc:
    def __init__(self) -> None:
        self.stdout = io.StringIO(ROW.format("read") + ROW.format("write"))
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:
        pass


def lstat_with(perf_mode: int = stat.S_IFREG | 0o755, perf_uid: int = 0):
    def lstat(path: str):
        if path == "/usr/bin/perf":
            return types.SimpleNamespace(st_uid=perf_uid, st_mode=perf_mode)
        return types.SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
    return lstat


class DeviceImcTests(unittest.TestCase):
    def run_sampler(self, lstat, interval=1.0):
        calls, procs = [], []

        def popen(cmd, **kw):
            calls.append((cmd, kw))
            procs.append(FakeProc())
            return procs[-1]

        imc = helper_main.device_imc(interval, lstat, popen=popen)
        with tempfile.TemporaryDirectory() as td:
            s = Sampler(None, hwmon_root=Path(td) / "h", powercap_root=Path(td) / "p", cpu_root=Path(td) / "c", imc=imc, interval_s=0.01)
            s.start()
            if imc._thread is not None:
                imc._thread.join(2)
            snap = s.collect()
            s.stop()
        return calls, procs, snap

    def test_trusted_perf_is_spawned_hardened_and_stopped_with_the_sampler(self):
        calls, procs, snap = self.run_sampler(lstat_with())
        (cmd, kw), = calls
        self.assertEqual(cmd[0], "/usr/bin/perf")
        self.assertEqual(kw["cwd"], "/")
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["env"], {"PATH": "/usr/sbin:/usr/bin", "LC_ALL": "C"})
        self.assertTrue(procs[0].terminated)  # Sampler.stop() stops perf: no orphan
        self.assertEqual(snap["dram"]["read"]["status"], "ok")
        self.assertAlmostEqual(snap["dram"]["read"]["value"], 1024 * 1.048576 / 1000)

    def test_untrusted_perf_is_not_spawned_and_dram_is_unavailable_with_a_reason(self):
        for lstat in (lstat_with(stat.S_IFREG | 0o775), lstat_with(stat.S_IFREG | 0o757), lstat_with(perf_uid=1000),
                      lstat_with(stat.S_IFLNK | 0o777)):
            calls, procs, snap = self.run_sampler(lstat)
            self.assertEqual(calls, [])
            for k in ("read", "write"):
                self.assertEqual(snap["dram"][k]["status"], "unavailable")
                self.assertIn("/usr/bin/perf", snap["dram"][k]["reason"])
            self.assertEqual(len(snap["cores"]), 24)  # telemetry as a whole continues

    def test_missing_perf_is_unavailable(self):
        def lstat(path):
            if path == "/usr/bin/perf":
                raise FileNotFoundError(path)
            return types.SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
        calls, _, snap = self.run_sampler(lstat)
        self.assertEqual(calls, [])
        self.assertEqual(snap["dram"]["read"]["status"], "unavailable")


class BuildConfigTests(unittest.TestCase):
    def test_device_sampler_owns_an_imc_and_sim_does_not(self):
        port = FakeMsrPort()
        port.close = lambda: None
        with mock.patch("arltuner.msr.DevCpuMsrPort", return_value=port):
            cfg = helper_main.build_config(ns(audit="/a", policy="/p"), log=lambda t: None)
            owner = cfg.sampler_factory(1.0)  # builds only; nothing is started, so perf is never run
        imc = owner._sampler._imc
        self.assertIsInstance(imc, ImcStat)
        self.assertEqual(imc.command[0], "/usr/bin/perf")
        self.assertEqual(imc.command[4], "1000")
        self.assertIsInstance(owner._sampler._msr, TelemetryMsr)
        sim = helper_main.build_config(ns(sim=True, device=False), log=lambda t: None)
        sampler = sim.sampler_factory(1.0)
        self.assertIsInstance(sampler, Sampler)
        self.assertIsNone(sampler._imc)


if __name__ == "__main__":
    unittest.main()
