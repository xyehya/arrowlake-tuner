"""Command builders (enumerated arguments only) and the output parsers on recorded outputs (no process)."""
from __future__ import annotations

import unittest

from arltuner.stress import MAX_ERROR_LINES, StressappScanner, StressNgScanner, clean_line, cpu_command, cpu_list, memory_command

from ._support import FAKEBIN


def scan(scanner, name: str):
    for line in (FAKEBIN / f"{name}.txt").read_text(encoding="utf-8").splitlines():
        scanner.feed(line)
    return scanner


class CommandTests(unittest.TestCase):
    def test_cpu_list_compacts_ranges(self) -> None:
        self.assertEqual(cpu_list([0, 1, 2, 3, 4, 5, 6, 7, 16, 17, 18, 19]), "0-7,16-19")
        self.assertEqual(cpu_list([5, 3, 3, 9]), "3,5,9")
        self.assertEqual(cpu_list(range(24)), "0-23")
        for bad in ([], [-1], [1024], [True], ["1"]):
            with self.assertRaises(ValueError):
                cpu_list(bad)  # type: ignore[arg-type]

    def test_cpu_commands_map_the_methods(self) -> None:
        cpus = [0, 1, 16, 17, 18, 19]
        self.assertEqual(
            cpu_command("/usr/bin/stress-ng", "integer", cpus, 60),
            ["/usr/bin/stress-ng", "--cpu", "6", "--taskset", "0-1,16-19", "--cpu-method", "int64", "--verify", "--metrics-brief", "--timeout", "60s"],
        )
        self.assertEqual(cpu_command("x", "vector-math", [3], 30)[1:7], ["--cpu", "1", "--taskset", "3", "--cpu-method", "matrixprod"])
        cache = cpu_command("x", "cache", [3, 4], 30)
        self.assertEqual(cache, ["x", "--cache", "2", "--taskset", "3-4", "--verify", "--metrics-brief", "--timeout", "30s"])
        self.assertNotIn("--cpu-method", cache)

    def test_the_memory_command(self) -> None:
        self.assertEqual(memory_command("/usr/bin/stressapptest", 120, 4096), ["/usr/bin/stressapptest", "-s", "120", "-M", "4096", "-W"])

    def test_anything_outside_the_enums_and_ranges_is_refused(self) -> None:
        for method in ("int64", "all", "integer; rm -rf /", "", None, 3):
            with self.assertRaises(ValueError):
                cpu_command("x", method, [0], 60)  # type: ignore[arg-type]
        for duration in (29, 3601, "60", 60.0, True, None):
            with self.assertRaises(ValueError):
                cpu_command("x", "integer", [0], duration)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                memory_command("x", duration, 1024)  # type: ignore[arg-type]
        for mb in (255, "1024", 1024.0, True, None):
            with self.assertRaises(ValueError):
                memory_command("x", 60, mb)  # type: ignore[arg-type]


class StressNgParseTests(unittest.TestCase):
    def test_a_clean_run(self) -> None:
        s = scan(StressNgScanner(), "stress-ng-ok")
        v = s.finish(0)
        self.assertEqual((v.errors, v.error_lines, v.ok, s.success), (0, [], True, True))
        self.assertFalse(s.finish(2).ok, "a non-zero exit is never ok")

    def test_a_verification_failure(self) -> None:
        s = scan(StressNgScanner(), "stress-ng-verify-fail")
        v = s.finish(2)
        self.assertEqual((v.errors, v.ok, s.failed), (1, False, 1))
        self.assertEqual(len(v.error_lines), 2)
        self.assertIn("matrixprod: data check failure", v.error_lines[0])

    def test_fail_lines_count_when_the_summary_never_came(self) -> None:
        s = StressNgScanner()
        s.feed("stress-ng: fail:  [9] cpu: int64: verification failure")
        s.feed("stress-ng: fail:  [10] cpu: int64: verification failure")
        self.assertEqual(s.finish(None).errors, 2)

    def test_garbage_is_ignored(self) -> None:
        s = StressNgScanner()
        for line in ("", "hello", "stress-ng:", "failed: 9", "stress-ng: info: [1] passed: 2: cpu (2)"):
            s.feed(line)
        self.assertEqual(s.finish(0).errors, 0)


class StressappParseTests(unittest.TestCase):
    def test_a_clean_run(self) -> None:
        s = scan(StressappScanner(), "stressapptest-ok")
        v = s.finish(0)
        self.assertEqual((v.errors, v.error_lines, v.ok, s.status), (0, [], True, "PASS"))

    def test_a_miscompare(self) -> None:
        s = scan(StressappScanner(), "stressapptest-miscompare")
        v = s.finish(1)
        self.assertEqual((v.errors, v.ok, s.status, s.incidents), (2, False, "FAIL", 2))
        self.assertTrue(any("miscompare on CPU 3" in line for line in v.error_lines))
        self.assertTrue(any("Status: FAIL" in line for line in v.error_lines))

    def test_a_fail_status_alone_is_at_least_one_error(self) -> None:
        s = StressappScanner()
        s.feed("2026/10/11-12:00:30(UTC) Status: FAIL - test discovered HW problems")
        self.assertEqual(s.finish(1).errors, 1)

    def test_error_lines_are_bounded_printable_and_short(self) -> None:
        s = StressNgScanner()
        for _ in range(100):
            s.feed("stress-ng: fail:  [1] cpu: boom \x1b[31m" + "x" * 500)
        v = s.finish(2)
        self.assertEqual(len(v.error_lines), MAX_ERROR_LINES)
        self.assertTrue(all(len(line) <= 200 and line.isprintable() for line in v.error_lines))
        self.assertEqual(v.errors, 100)
        self.assertEqual(clean_line("a\x00b\n"), "a?b")


if __name__ == "__main__":
    unittest.main()
