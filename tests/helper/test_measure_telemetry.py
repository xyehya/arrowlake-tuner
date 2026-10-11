"""``telemetry.start`` restart rule (contract 9): same interval attaches, another interval stops the sampler and starts it again."""
from __future__ import annotations

import threading
import unittest
from typing import Any

from arltuner.telemetry import Sampler, TelemetryMsr

from ._support import HelperCase, error_code


class CountingSampler:
    """Wraps a real sampler; the class records every start and stop so a restart is visible."""

    instances: list["CountingSampler"] = []

    def __init__(self, inner: Sampler, interval: float) -> None:
        self.inner, self.interval = inner, interval
        self.starts = self.stops = 0
        CountingSampler.instances.append(self)

    def start(self) -> None:
        self.starts += 1
        self.inner.start()

    def stop(self) -> None:
        self.stops += 1
        self.inner.stop()

    def snapshot(self) -> Any:
        return self.inner.snapshot()


class TelemetryRestartTests(HelperCase):
    def setUp(self) -> None:
        super().setUp()
        CountingSampler.instances = []
        self.created: list[CountingSampler] = []

    def cfg(self, fail_second: bool = False):
        def factory(interval: float) -> CountingSampler:
            if fail_second and self.created:
                raise RuntimeError("no sampler")
            s = CountingSampler(Sampler(TelemetryMsr(self.msr_for_telemetry), hwmon_root=self.tmp / "h", powercap_root=self.tmp / "p", interval_s=interval), interval)
            self.created.append(s)
            return s

        return self.config(sampler_factory=factory)

    def test_a_different_interval_restarts_the_sampler(self) -> None:
        h = self.start(self.cfg())
        first = h.call("t1", "telemetry.start", {"interval_s": 1})["result"]
        self.assertEqual((first["running"], first["already_running"], first["restarted"], first["interval_s"], first["op_id"]), (True, False, False, 1.0, "t1"))
        again = h.call("t2", "telemetry.start", {"interval_s": 1})["result"]
        self.assertEqual((again["already_running"], again["restarted"], again["op_id"]), (True, False, "t1"))  # attach, as before
        self.assertEqual(len(self.created), 1)
        fast = h.call("t3", "telemetry.start", {"interval_s": 0.25})["result"]
        self.assertEqual((fast["running"], fast["restarted"], fast["already_running"], fast["interval_s"], fast["op_id"]), (True, True, False, 0.25, "t3"))
        self.assertEqual([(s.interval, s.starts, s.stops) for s in self.created], [(1.0, 1, 1), (0.25, 1, 0)])  # the old one stopped, the new one started
        ev = h.wait_for(lambda m: m.get("event") == "telemetry" and m["op_id"] == "t3")  # events now carry the new request id
        self.assertEqual(ev["op_id"], "t3")
        slow = h.call("t4", "telemetry.start", {"interval_s": 5})["result"]
        self.assertEqual((slow["restarted"], slow["interval_s"]), (True, 5.0))
        self.assertEqual(h.call("s", "telemetry.stop")["result"], {"stopped": True, "op_id": "t4"})
        self.assertEqual([(s.starts, s.stops) for s in self.created], [(1, 1), (1, 1), (1, 1)])  # no sampler left running

    def test_the_old_sampler_stream_ends_when_it_is_replaced(self) -> None:
        h = self.start(self.cfg())
        h.call("t1", "telemetry.start", {"interval_s": 0.2})
        h.wait_for(lambda m: m.get("event") == "telemetry" and m["op_id"] == "t1")
        h.call("t2", "telemetry.start", {"interval_s": 0.4})
        mark = len(h.messages)
        h.wait_for(lambda m: m.get("event") == "telemetry" and m["op_id"] == "t2", start=mark)
        mark = len(h.messages)
        h.wait_for(lambda m: m.get("event") == "heartbeat", start=mark)
        h.wait_for(lambda m: m.get("event") == "heartbeat", start=len(h.messages))
        self.assertFalse([m for m in h.messages[mark:] if m.get("event") == "telemetry" and m.get("op_id") == "t1"])

    def test_a_restart_that_cannot_start_reports_it_and_leaves_telemetry_off(self) -> None:
        h = self.start(self.cfg(fail_second=True))
        h.call("t1", "telemetry.start", {"interval_s": 1})
        reply = h.call("t2", "telemetry.start", {"interval_s": 2})
        self.assertEqual(error_code(reply), "INTERNAL")
        self.assertIn("previous sampler was stopped", reply["error"]["message"])
        self.assertEqual((self.created[0].starts, self.created[0].stops), (1, 1))
        self.assertEqual(h.call("s", "telemetry.stop")["result"], {"stopped": False})

    def test_concurrent_restarts_never_leave_two_samplers(self) -> None:
        h = self.start(self.cfg())
        threads = [threading.Thread(target=lambda i=i: h.call(f"c{i}", "telemetry.start", {"interval_s": 0.25 + i})) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        h.call("s", "telemetry.stop")
        self.assertTrue(all(s.starts == s.stops for s in self.created), [(s.starts, s.stops) for s in self.created])


if __name__ == "__main__":
    unittest.main()
