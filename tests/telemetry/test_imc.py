import io
import subprocess
import unittest

from arltuner.telemetry import imc

# Recorded-format fixture written from the documented `perf stat -a -I 1000 -x ,` CSV layout (perf-stat(1)); stderr text
# is interleaved because perf prints to stderr. Two IMC instances, two intervals, then a not-counted interval.
FIXTURE = """\
#           time             counts unit events
     1.000117654,1024.00,MiB,uncore_imc_free_running_0/data_read/,1000117654,100.00,,
     1.000117654,512.00,MiB,uncore_imc_free_running_0/data_write/,1000117654,100.00,,
     1.000117654,1024.00,MiB,uncore_imc_free_running_1/data_read/,1000117654,100.00,,
     1.000117654,0.00,MiB,uncore_imc_free_running_1/data_write/,1000117654,100.00,,
     2.000234111,2048.00,MiB,uncore_imc_free_running_0/data_read/,1000116457,100.00,,
     2.000234111,256.00,MiB,uncore_imc_free_running_0/data_write/,1000116457,100.00,,
Warning: some perf noise on stderr
     3.000351000,<not counted>,MiB,uncore_imc_free_running_0/data_read/,0,0.00,,
     3.000351000,<not supported>,MiB,uncore_imc_free_running_0/data_write/,0,0.00,,
"""


class Parse(unittest.TestCase):
    def test_rows(self):
        r = imc.parse_row("     1.000117654,1024.00,MiB,uncore_imc_free_running_0/data_read/,1000117654,100.00,,")
        self.assertEqual((r.timestamp, r.count, r.unit), (1.000117654, 1024.0, "MiB"))
        self.assertIsNone(imc.parse_row("Warning: some perf noise on stderr"))
        self.assertIsNone(imc.parse_row("# time,counts"))
        self.assertIsNone(imc.parse_row(""))
        self.assertIsNone(imc.parse_row("1.0,<not counted>,MiB,ev,0,0.00,,").count)

    def test_stream(self):
        res = imc.parse_stream(FIXTURE.splitlines(), 1.0)
        self.assertEqual(len(res), 3)
        rd, wr = res[0]
        self.assertAlmostEqual(rd.value, 2048 * 1.048576 / 1000)
        self.assertAlmostEqual(wr.value, 512 * 1.048576 / 1000)
        self.assertAlmostEqual(res[1][0].value, 2048 * 1.048576 / 1000)
        self.assertFalse(res[2][0].available)
        self.assertFalse(res[2][1].available)

    def test_missing_event_is_unavailable(self):
        res = imc.parse_stream(["1.0,100.00,MiB,x/data_read/,1,100.00,,"], 1.0)
        self.assertTrue(res[0][0].available)
        self.assertFalse(res[0][1].available)


class FakeProc:
    def __init__(self, text):
        self.stdout = io.StringIO(text)
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class Process(unittest.TestCase):
    def test_single_process_started_once_and_latest(self):
        spawned = []

        def popen(cmd, **kw):
            spawned.append(cmd)
            return FakeProc(FIXTURE)

        s = imc.ImcStat(1000, popen=popen)
        self.assertFalse(s.latest()[0].available)
        s.start()
        s.start()
        s._thread.join(2)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][:6], ["/usr/bin/perf", "stat", "-a", "-I", "1000", "-x"])
        self.assertFalse(s.latest()[0].available)  # last interval of the fixture is not-counted
        s.stop()

    def test_spawn_is_hardened(self):
        calls = []

        def popen(cmd, **kw):
            calls.append((cmd, kw))
            return FakeProc("")

        s = imc.ImcStat(1000, popen=popen)
        s.start()
        s._thread.join(2)
        s.stop()
        (cmd, kw), = calls
        self.assertEqual(cmd[0], "/usr/bin/perf")
        self.assertIn("uncore_imc_free_running/data_read/,uncore_imc_free_running/data_write/", cmd)
        self.assertEqual(kw["env"], {"PATH": "/usr/sbin:/usr/bin", "LC_ALL": "C"})  # nothing inherited
        self.assertEqual(kw["cwd"], "/")
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["stderr"], subprocess.STDOUT)

    def test_trust_failure_means_no_spawn_and_a_reason(self):
        spawned, checked = [], []

        def refuse(path):
            checked.append(path)
            raise imc.PerfRefused("/usr/bin/perf: mode 0777 is group- or world-writable")

        s = imc.ImcStat(1000, popen=lambda cmd, **kw: spawned.append(cmd), trust_check=refuse)
        s.start()
        self.assertEqual((spawned, checked), ([], ["/usr/bin/perf"]))
        for r in s.latest():
            self.assertIsNone(r.value)
            self.assertIn("world-writable", r.reason)
        s.stop()  # nothing to stop

    def test_trusted_perf_is_spawned_and_stop_terminates_it(self):
        procs = []

        def popen(cmd, **kw):
            procs.append(FakeProc(""))
            return procs[-1]

        s = imc.ImcStat(1000, popen=popen, trust_check=lambda path: None)
        s.start()
        s.stop()
        self.assertEqual(len(procs), 1)
        self.assertTrue(procs[0].terminated)

    def test_perf_missing(self):
        def popen(cmd, **kw):
            raise FileNotFoundError("perf")

        s = imc.ImcStat(popen=popen)
        s.start()
        rd, wr = s.latest()
        self.assertIsNone(rd.value)
        self.assertIn("cannot start perf", rd.reason)


if __name__ == "__main__":
    unittest.main()
