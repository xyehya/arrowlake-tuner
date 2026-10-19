"""G-240: Undervolt Protection (rc 0x13) through the helper (simulator): the refusal is a named one, this boot remembers it, and the planner works around it.

``[MEASURED lnx 2026-10-19 13:12Z]`` (docs/knowledge/registers.md): after a BIOS reset to defaults the first per-point write with a negative offset answered rc 0x13.
No hardware: ``SimConfig(uvp=True)`` answers every 0x11 with a negative offset that way.
"""
from __future__ import annotations

import dataclasses

from arltuner.helper.ops_tune import UVP_CHECK, UVP_MESSAGE, UVP_UNKNOWN_NOTE
from arltuner.msr import SimConfig

from .test_tune import TuneCase, row, rows, texts

BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def uvp_sim(on: bool = True) -> SimConfig:
    return dataclasses.replace(SimConfig.measured(per_point=True, reject_zero_fields=True), uvp=on)


class UndervoltProtection(TuneCase):
    def refused_apply(self, h):
        r = self.apply(h, self.plan(h, rows(row("P0", points={"53": -10}))))
        self.assertFalse(r["ok"], r)
        return r["error"]

    def test_read_reports_unknown_before_any_apply(self) -> None:
        h = self.boot(uvp_sim())
        self.assertEqual(self.read(h)["uvp"], "unknown")

    def test_a_negative_offset_plan_carries_the_unknown_note_only_when_it_has_one(self) -> None:
        h = self.boot(uvp_sim())
        self.read(h)
        self.assertIn(UVP_UNKNOWN_NOTE, texts(self.plan(h, rows(row("P0", points={"53": -10})))))
        self.assertNotIn(UVP_UNKNOWN_NOTE, texts(self.plan(h, rows(row("P0", points={"53": 10})))))

    def test_the_refusal_is_named_and_the_word_is_unchanged(self) -> None:
        h = self.boot(uvp_sim())
        self.read(h)
        err = self.refused_apply(h)
        self.assertEqual((err["code"], err["message"], err["detail"]["check"]), ("REFUSED", UVP_MESSAGE, UVP_CHECK))
        self.assertEqual(self.word(0, 5).offset_raw, 0)
        self.assertEqual(self.read(h)["uvp"], "on")

    def test_next_plan_refuses_negative_offsets_before_any_write(self) -> None:
        h = self.boot(uvp_sim())
        self.read(h)
        self.refused_apply(h)
        sent = len(self.sim.requests)
        for n, (intent, text) in enumerate((
            (rows(row("P0", points={"53": -10})), "P0: offset -10 mV"),
            (rows(row("P0", curve=-20)), "P0: offset -20 mV"),
            (rows(row("P0", points={"53": 0}), row("P1", points={"53": -5})), "P1: offset -5 mV"),
        )):
            r = h.call(f"p{n}", "tune.plan", {"intent": intent})
            self.assertFalse(r["ok"], r)
            self.assertEqual((r["error"]["code"], r["error"]["detail"]["check"]), ("REFUSED", UVP_CHECK))
            self.assertTrue(r["error"]["message"].startswith(text + ": "), r["error"]["message"])
            self.assertIn(UVP_MESSAGE, r["error"]["message"])
        self.assertEqual(len(self.sim.requests), sent)  # planning sends nothing

    def test_positive_offsets_and_ratios_still_plan_and_apply(self) -> None:
        h = self.boot(uvp_sim())
        self.read(h)
        self.refused_apply(h)
        res = self.apply_ok(h, self.plan(h, rows(row("P0", curve=10))))
        self.assertEqual(res["uvp"], "on")
        self.assertGreater(self.word(0).offset_raw, 0)
        self.assertEqual(self.plan(h, rows(row("P0", ratio=57)))["uvp"], "on")

    def test_the_flag_survives_a_helper_restart_and_a_new_read(self) -> None:
        h = self.boot(uvp_sim(), boot_id_fn=lambda: BOOT)
        self.read(h)
        self.refused_apply(h)
        h.stop()
        again = self.boot(uvp_sim(), boot_id_fn=lambda: BOOT)
        self.assertEqual(again.call("last", "tune.last", {})["result"]["result"]["uvp"], "on")
        self.assertEqual(self.read(again)["uvp"], "on")  # a new read keeps it
        r = again.call("plan", "tune.plan", {"intent": rows(row("P0", points={"53": -10}))})
        self.assertEqual(r["error"]["detail"]["check"], UVP_CHECK)

    def test_another_boot_starts_unknown(self) -> None:
        h = self.boot(uvp_sim(), boot_id_fn=lambda: BOOT)
        self.read(h)
        self.refused_apply(h)
        h.stop()
        later = self.boot(uvp_sim(), boot_id_fn=lambda: "ffffffff-0000-0000-0000-000000000000")
        self.assertEqual(self.read(later)["uvp"], "unknown")

    def test_a_negative_offset_that_reads_back_marks_it_off(self) -> None:
        h = self.boot(uvp_sim(on=False))
        self.read(h)
        res = self.apply_ok(h, self.plan(h, rows(row("P0", points={"53": -10}))))
        self.assertEqual(res["uvp"], "off")
        self.assertEqual(self.read(h)["uvp"], "off")
        self.assertNotIn(UVP_UNKNOWN_NOTE, texts(self.plan(h, rows(row("P0", points={"53": -20})))))

    def test_a_positive_apply_leaves_it_unknown(self) -> None:
        h = self.boot(uvp_sim())
        self.read(h)
        self.assertEqual(self.apply_ok(h, self.plan(h, rows(row("P0", curve=10))))["uvp"], "unknown")

    def test_a_refusal_after_other_words_landed_is_a_partial_result_with_the_message(self) -> None:
        h = self.boot(uvp_sim())
        self.read(h)
        r = self.apply(h, self.plan(h, rows(row("P0", curve=10), row("P1", curve=-10))))
        self.assertTrue(r["ok"], r)
        res = r["result"]
        self.assertFalse(res["complete"])
        self.assertEqual((res["stop"]["reason"], res["stop"]["check"], res["uvp"]), (UVP_MESSAGE, UVP_CHECK, "on"))
        self.assertTrue(any(rb["match"] for rb in res["readback"]))
