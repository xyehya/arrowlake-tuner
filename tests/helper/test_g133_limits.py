"""G-133 (D-036, D-049): ``tune.read`` / ``tune.last`` carry ``limits`` -- the shape the page reads. D-049 removed every operator value limit, so the policy part is empty (offset window =
the 11-bit field, ratio / V target null with source ``none``); the CPU's own limits are in ``hw_limits`` / ``cpu_limits``.

Through the real helper loop and the simulator; temp directories only. No hardware, no sudo."""
from __future__ import annotations

from typing import Any

from .test_continuity import LastReadCase


class LimitsTests(LastReadCase):
    def test_the_read_carries_no_operator_value_limits(self) -> None:
        h = self.boot()
        res = self.read(h)
        lim = res["limits"]
        self.assertEqual(lim["offset_mv"], {"P": {"min": -1000, "max": 999}, "E": {"min": -1000, "max": 999}})
        self.assertEqual(lim["v_target_max_v"], {"P": None, "E": None})
        self.assertEqual(lim["ratio_max"], {"P": None, "E": None})
        self.assertEqual(lim["source"], {"ratio_max": {"P": "none", "E": "none"}, "v_target_max": {"P": "none", "E": "none"}})
        self.assertEqual(res["cpu_limits"]["P"]["max_ratio"], 120)  # the CPU's own limit is where the page reads it now

    def test_an_old_policy_with_the_removed_value_limits_still_loads_and_changes_nothing(self) -> None:
        text = self.policy.read_text()
        top = "effective_offset_floor_mv = -120\nassumed_bios_offset_mv = { P = -20, E = 0 }\nmax_ratio_step = 2\n"  # top-level keys come before the first table
        tables = "\n[ratio_max]\nP = 60\nE = 49\n[v_target_max_mv]\nP = 1300\nE = 1300\n"
        first = text.index("[[")
        self.policy.write_text(text[:first] + top + text[first:] + tables)
        h = self.boot()
        lim = self.read(h)["limits"]
        self.assertEqual((lim["offset_mv"]["P"]["min"], lim["offset_mv"]["E"]["min"]), (-1000, -1000))
        self.assertEqual((lim["ratio_max"]["P"], lim["v_target_max_v"]["P"]), (None, None))

    def test_tune_last_still_carries_the_limits_after_a_helper_restart(self) -> None:
        h = self.boot()
        self.read(h)
        h2 = self.restart(h)
        last = self.last(h2)["result"]  # nothing was read in this process: the stored read decides
        self.assertEqual(last["limits"]["offset_mv"]["P"], {"min": -1000, "max": 999})
        self.assertEqual(last["cpu_limits"]["P"]["max_ratio"], 120)

    def test_an_unloadable_policy_gives_null_limits_and_a_note_but_no_error(self) -> None:
        h = self.boot()
        self.read(h)
        self.policy.write_text("this is not toml = = =")
        h2 = self.restart(h)
        got: dict[str, Any] = self.last(h2)
        self.assertTrue(got["available"])
        self.assertIsNone(got["result"]["limits"])
        self.assertIn("policy limits not available", got["result"]["limits_note"])
