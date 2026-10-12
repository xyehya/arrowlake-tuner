"""G-133 (D-036): ``tune.read`` / ``tune.last`` carry ``limits`` -- the bound policy with ``"auto"`` resolved -- so the page offers what the guard accepts and holds no literal.

Through the real helper loop and the simulator; temp directories only. No hardware, no sudo."""
from __future__ import annotations

from typing import Any

from .test_continuity import LastReadCase


class LimitsTests(LastReadCase):
    def edit_policy(self, old: str, new: str) -> None:
        text = self.policy.read_text()
        self.assertIn(old, text)
        self.policy.write_text(text.replace(old, new))

    def test_the_read_carries_the_limits_of_the_shipped_policy(self) -> None:
        h = self.boot()
        lim = self.read(h)["limits"]
        self.assertEqual(lim["offset_mv"], {"P": {"min": -200, "max": 200}, "E": {"min": -200, "max": 200}})   # policies/default.toml: ±200 mV (D-032)
        self.assertEqual(lim["v_target_max_v"], {"P": 1.625, "E": 1.355})   # "auto": the sanity bound of the kind; the CPU's own 0x28 maximum is checked at apply
        self.assertEqual(lim["ratio_max"]["E"], 49)   # the test policy states E = 49
        self.assertEqual(lim["ratio_max"]["P"], 57)   # "auto": the 0x1C words read this boot
        self.assertEqual(lim["source"], {"ratio_max": {"P": "auto", "E": "policy"}, "v_target_max": {"P": "auto", "E": "auto"}})

    def test_a_number_in_the_policy_is_the_limit(self) -> None:
        self.edit_policy('[ratio_max]\nP = "auto"\nE = 49', "[ratio_max]\nP = 60\nE = 49")
        h = self.boot()
        lim = self.read(h)["limits"]
        self.assertEqual((lim["ratio_max"]["P"], lim["source"]["ratio_max"]["P"]), (60, "policy"))

    def test_no_ceiling_in_the_policy_is_none_not_a_guess(self) -> None:
        self.edit_policy('[v_target_max_mv]\nP = "auto"\nE = "auto"', "[v_target_max_mv]\n")
        h = self.boot()
        lim = self.read(h)["limits"]
        self.assertEqual(lim["v_target_max_v"], {"P": None, "E": None})
        self.assertEqual(lim["source"]["v_target_max"], {"P": "none", "E": "none"})

    def test_the_offset_floor_follows_the_policy(self) -> None:
        self.edit_policy("effective_offset_floor_mv = -200", "effective_offset_floor_mv = -120")
        self.edit_policy("assumed_bios_offset_mv = { P = 0, E = 0 }", "assumed_bios_offset_mv = { P = -20, E = 0 }")
        h = self.boot()
        lim = self.read(h)["limits"]
        self.assertEqual((lim["offset_mv"]["P"]["min"], lim["offset_mv"]["E"]["min"]), (-100, -120))   # the floor of BIOS + runtime minus the BIOS offset

    def test_tune_last_recomputes_the_limits_from_the_policy_as_it_is_now(self) -> None:
        h = self.boot()
        self.read(h)
        self.edit_policy('[ratio_max]\nP = "auto"\nE = 49', '[ratio_max]\nP = "auto"\nE = 52')
        h2 = self.restart(h)
        last = self.last(h2)["result"]
        self.assertEqual(last["limits"]["ratio_max"]["E"], 52)

    def test_tune_last_after_a_helper_restart_still_resolves_the_auto_ratio(self) -> None:
        h = self.boot()
        self.read(h)
        h2 = self.restart(h)
        last = self.last(h2)["result"]   # nothing was read in this process: the 0x1C words stored with the read decide
        self.assertEqual(last["limits"]["ratio_max"]["P"], 57)

    def test_an_unloadable_policy_gives_null_limits_and_a_note_but_no_error(self) -> None:
        h = self.boot()
        self.read(h)
        self.policy.write_text("this is not toml = = =")
        h2 = self.restart(h)
        got: dict[str, Any] = self.last(h2)
        self.assertTrue(got["available"])
        self.assertIsNone(got["result"]["limits"])
        self.assertIn("policy limits not available", got["result"]["limits_note"])
