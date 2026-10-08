"""Windows WHEA-Logger health gate against a fake ``wevtutil`` runner; the real one is never started."""
from __future__ import annotations

import unittest

from arltuner.health import make_whea_health_gate


def xml(*ids: int) -> str:
    return "".join(f"<Event xmlns='x'><System><EventRecordID>{i}</EventRecordID></System></Event>\n" for i in ids)


class Runner:
    def __init__(self, *results: tuple[int, str]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv):  # type: ignore[no-untyped-def]
        self.calls.append(list(argv))
        return self.results.pop(0)


class WheaGate(unittest.TestCase):
    def test_clean_bracket_with_and_without_old_events(self) -> None:
        for old in ((), (5, 6)):
            gate = make_whea_health_gate(Runner((0, xml(*old)), (0, xml(*old))))
            self.assertEqual(gate("before")[0], True)
            self.assertEqual(gate("after")[0], True)

    def test_query_shape(self) -> None:
        run = Runner((0, ""))
        make_whea_health_gate(run)("before")
        argv = run.calls[0]
        self.assertEqual(argv[:3], ["wevtutil", "qe", "System"])
        self.assertIn("/q:*[System[Provider[@Name='Microsoft-Windows-WHEA-Logger']]]", argv)
        self.assertIn("/rd:true", argv)

    def test_new_event_fails_after(self) -> None:
        gate = make_whea_health_gate(Runner((0, xml(5)), (0, xml(9, 5))))
        gate("before")
        ok, detail = gate("after")
        self.assertFalse(ok)
        self.assertIn("1 new", detail)
        self.assertIn("9", detail)

    def test_log_rollover_that_drops_old_events_is_not_a_failure(self) -> None:
        gate = make_whea_health_gate(Runner((0, xml(5, 6)), (0, xml(6))))
        gate("before")
        self.assertTrue(gate("after")[0])

    def test_failing_query_fails_closed(self) -> None:
        gate = make_whea_health_gate(Runner((5, "Access is denied"), (0, "")))
        self.assertFalse(gate("before")[0])
        self.assertFalse(gate("after")[0])  # and no stale snapshot is used

    def test_after_query_failure_fails(self) -> None:
        gate = make_whea_health_gate(Runner((0, ""), (1, "")))
        gate("before")
        self.assertFalse(gate("after")[0])

    def test_runner_exception_and_garbage_fail_closed(self) -> None:
        def boom(argv):  # type: ignore[no-untyped-def]
            raise FileNotFoundError("wevtutil")

        self.assertFalse(make_whea_health_gate(boom)("before")[0])
        self.assertFalse(make_whea_health_gate(Runner((0, "<Event><System></System></Event>")))("before")[0])

    def test_after_without_before_and_unknown_phase(self) -> None:
        gate = make_whea_health_gate(Runner((0, "")))
        self.assertFalse(gate("after")[0])
        self.assertFalse(gate("during")[0])


if __name__ == "__main__":
    unittest.main()
