"""G-212 (D-045, D-046): the ring as a ``tune.plan`` intent form, ``{ring: {points: {"<P2>": mV}}}`` (alone, or beside ``{scope, rows[, turbo]}``).

Simulator flows through the real helper loop and the REAL guard (no hardware). Ring words of the reference machine: OC word ``0x28``, points 1..8 = 8/14/20/28/34/37/38/39x, P2 = 9 = 40x.
The ring OC word is BIOS-owned: never written; a ring OC word that is not the one read refuses the ring unit alone; the policy window of ``0x11 P1=2`` bounds every point.
"""
from __future__ import annotations

from typing import Any

from arltuner.helper.ops_tune import INTENT_FEATURES, canonical_intent
from arltuner.helper.protocol import OpError
from arltuner.mailbox import Domain, VfWord

from ._support import error_code
from .test_complete_profiles import COLD, CompleteCase
from .test_ring_accepted import WARM_RING_OC_27, ring_requests
from .test_tune import row, rows


class RingIntentCase(CompleteCase):
    def plan_error(self, h: Any, intent: dict[str, Any]) -> dict[str, Any]:
        r = h.call(f"plan{len(h.messages)}", "tune.plan", {"intent": intent})
        self.assertFalse(r["ok"], r)
        return r["error"]


class Canonical(RingIntentCase):
    def test_the_canonical_form_sorts_and_normalises_the_points(self) -> None:
        self.assertEqual(canonical_intent({"ring": {"points": {"04": -25, "1": 0}}}), {"ring": {"points": {"1": 0, "4": -25}}})
        both = canonical_intent({"scope": "unit", "rows": [row("P0", curve=-10)], "ring": {"points": {"4": -25}}})
        self.assertEqual(sorted(both), ["ring", "rows", "scope"])

    def test_malformed_ring_intents_are_bad_requests(self) -> None:
        for bad in (
            {"ring": {"points": {"0": -25}}},  # P2 = 0 is the ring OC word: never written
            {"ring": {"points": {"16": -25}}},
            {"ring": {"points": {"x": -25}}},
            {"ring": {"points": {}}},
            {"ring": {"points": {"4": -25}, "oc": 40}},
            {"ring": {"points": {"4": -25}, "ratio": 40}},
            {"ring": {"points": {"4": 1.5}}},
            {"ring": {"points": {"4": -1001}}},
            {"ring": {"points": [4]}},
            {"ring": {}},
            {"ring": None},
            {"ring": {"points": {"4": -25}}, "profile": "x"},
            {"ring": {"points": {"4": -25}}, "scope": "unit"},  # a scope without rows
        ):
            with self.subTest(bad=bad), self.assertRaises(OpError) as caught:
                canonical_intent(bad)
            self.assertEqual(caught.exception.code, "BAD_REQUEST")

    def test_point_keys_are_one_or_two_ascii_digits_and_unique(self) -> None:
        for bad in (
            {"ring": {"points": {"\u00b2": -25}}},  # superscript two: str.isdigit() is true, int() fails
            {"ring": {"points": {"04": -25, "4": -25}}},  # the same point twice after normalising
            {"ring": {"points": {"4.0": -25}}},
            {"ring": {"points": {" 4": -25}}},
            {"ring": {"points": {"-1": -25}}},
            {"ring": {"points": {"": -25}}},
            {"ring": {"points": {"1" * 5000: -25}}},
            {"ring": {"points": {"\u0664": -25}}},  # Arabic-indic four
        ):
            with self.subTest(bad=repr(bad)[:60]), self.assertRaises(OpError) as caught:
                canonical_intent(bad)
            self.assertEqual(caught.exception.code, "BAD_REQUEST")

    def test_a_ring_intent_cannot_be_saved_as_a_profile(self) -> None:
        with self.assertRaises(OpError):
            canonical_intent({"scope": "unit", "rows": [row("P0", curve=-10)], "ring": {"points": {"4": -25}}}, rows_only=True)
        with self.assertRaises(OpError):
            canonical_intent({"ring": {"points": {"4": -25}}}, rows_only=True)


class Plan(RingIntentCase):
    def test_the_capability_flag_is_in_the_read_and_in_last(self) -> None:
        h = self.boot(COLD)
        self.assertEqual(self.read(h)["intent_features"], list(INTENT_FEATURES))
        self.assertIn("ring", INTENT_FEATURES)
        r = h.call("last", "tune.last", {})
        self.assertEqual(r["result"]["result"]["intent_features"], ["ring"])

    def test_one_ring_point_is_planned_as_one_ring_write_within_policy(self) -> None:
        h = self.boot(COLD)
        self.read(h)
        sent = len(self.sim.requests)
        plan = self.plan(h, {"ring": {"points": {"4": -25}}})
        self.assertEqual(plan["units"], ["ring"])
        writes = [s for s in plan["steps"] if s["kind"].startswith("write")]
        self.assertEqual([(s["kind"], s["unit"], s["point"], s["offset_mv"]) for s in writes], [("write_ring_point", "ring", 4, -25)])
        self.assertEqual(plan["after"]["ring"]["4"]["offset_mv"], -25.391)  # -25 mV in the field's 1/1024 V steps
        self.assertEqual(plan["before"]["ring"]["4"]["offset_mv"], 0.0)
        self.assertEqual([w for w in plan["warnings"] if w["code"] == "RING_NOT_RESTORED"], [])
        self.assertEqual(len(self.sim.requests), sent, "a plan sends nothing")

    def test_apply_writes_exactly_that_ring_point_and_nothing_else(self) -> None:
        h = self.boot(COLD)
        self.read(h)
        oc_before = self.sim.vf(0, int(Domain.RING), 0).to_u32()
        core_before = self.sim.vf(0, 0, 4).to_u32()
        start = len(self.sim.requests)
        result = self.apply_ok(h, self.plan(h, {"ring": {"points": {"4": -25}}}))
        writes = [r for r in ring_requests(self.sim, 0x11, start)]
        self.assertEqual([(r.cpu, r.p2) for r in writes], [(0, 4)])
        self.assertEqual(VfWord.from_u32(writes[0].data).offset_mv, -25.390625)
        self.assertEqual(VfWord.from_u32(writes[0].data).ratio, 28)  # the point's own ratio, read: only the offset moved
        self.assertEqual(self.sim.vf(0, int(Domain.RING), 4).offset_raw, -26)
        self.assertEqual(self.sim.vf(0, int(Domain.RING), 0).to_u32(), oc_before, "the ring OC word is never written")
        self.assertEqual(self.sim.vf(0, 0, 4).to_u32(), core_before, "P-core 0 point 4 is not ring point 4")
        self.assertFalse([r for r in self.sim.requests[start:] if r.cmd in (0x11, 0x15, 0x1D, 0x27) and r.p1 != 2])
        self.assertEqual([(r["unit"], r["point"], r["match"]) for r in result["readback"]], [("ring", 4, True)])
        self.assertEqual(result["ring_refused"], [])
        again = self.read(h)
        self.assertEqual(again["ring"]["points"][3]["offset_mv"], -25.391)

    def test_ring_beside_rows_plans_both_units(self) -> None:
        h = self.boot(COLD)
        self.read(h)
        plan = self.plan(h, {**rows(row("P0", curve=-10)), "ring": {"points": {"4": -25, "9": -10}}})
        self.assertEqual(plan["units"], ["P0", "ring"])
        kinds = sorted((s["kind"], s["point"]) for s in plan["steps"] if s["kind"] == "write_ring_point")
        self.assertEqual(kinds, [("write_ring_point", 4), ("write_ring_point", 9)])
        self.apply_ok(h, plan)
        self.assertNotEqual(self.sim.vf(0, 0, 0).offset_raw, 0, "the P0 row was written")
        self.assertEqual(sorted(r.p2 for r in ring_requests(self.sim, 0x11)), [4, 9])

    def test_no_ring_read_is_a_no_read_refusal(self) -> None:
        h = self.boot(COLD)
        sent = len(self.sim.requests)
        err = self.plan_error(h, {"ring": {"points": {"4": -25}}})
        self.assertEqual((err["code"], err["detail"]["check"]), ("REFUSED", "NO_READ"))
        self.assertEqual(len(self.sim.requests), sent)

    def test_a_point_the_read_does_not_have_is_a_bad_request(self) -> None:
        h = self.boot(WARM_RING_OC_27)  # the BIOS ring ratio is 39: no P2 = 9 (40x) slot, though the policy entry covers it
        self.assertEqual(len(self.read(h)["ring"]["points"]), 8)
        err = self.plan_error(h, {"ring": {"points": {"9": -5}}})
        self.assertEqual(err["code"], "BAD_REQUEST")
        self.assertIn("1..8", err["message"])

    def test_a_ring_offset_has_no_policy_window_only_the_field_and_warns(self) -> None:
        """D-049: the old policy window (runtime floor / offset ceiling) is gone -- -300 / +300 mV fit the 11-bit field, so they are planned with warnings."""
        h = self.boot(COLD)
        self.read(h)
        for offset, code in ((-300, "LARGE_CHANGE"), (300, "POSITIVE_OFFSET")):
            with self.subTest(offset=offset):
                plan = self.plan(h, {"ring": {"points": {"4": offset}}})
                self.assertEqual(plan["units"], ["ring"])
                self.assertIn(code, [w["code"] for w in plan["warnings"]])

    def test_a_ring_offset_past_the_field_is_refused(self) -> None:
        h = self.boot(COLD)
        self.read(h)
        r = h.call("ringbig", "tune.plan", {"intent": {"ring": {"points": {"4": -1001}}}})
        self.assertFalse(r["ok"], r)
        self.assertIn("-1000..999", r["error"]["message"])

    def test_a_ring_point_without_a_policy_write_entry_is_refused_by_name(self) -> None:
        h = self.boot(COLD)
        self.read(h)
        err = self.plan_error(h, {"ring": {"points": {"10": -5}}})  # the shipped ring entry is P2 1..9
        self.assertEqual((err["code"], err["detail"]["check"]), ("REFUSED", "POLICY_RANGE"))
        self.assertIn("no write entry for cmd 0x11 P1=2 P2=10", err["message"])


class RingOcWordChanged(RingIntentCase):
    def test_a_changed_ring_oc_word_refuses_the_ring_alone(self) -> None:
        h = self.boot(COLD)
        self.read(h)
        plan = self.plan(h, {**rows(row("P0", curve=-10)), "ring": {"points": {"4": -25}}})
        self.sim._vf[(int(Domain.RING), -1, 0)] = VfWord.from_u32(0x27)  # the BIOS ring ratio changed after the read (test double; no mailbox write)
        result = self.apply_ok(h, plan)
        self.assertTrue(any("ring_oc_differs" in why for why in result["ring_refused"]), result["ring_refused"])
        self.assertEqual(ring_requests(self.sim, 0x11), [], "no ring word is written")
        self.assertEqual(self.sim.vf(0, int(Domain.RING), 0).to_u32(), 0x27, "the ring OC word is left as the BIOS has it")
        self.assertNotEqual(self.sim.vf(0, 0, 0).offset_raw, 0, "the P-core row of the same plan was written")

    def test_a_reread_after_the_change_builds_the_intent_from_the_new_word(self) -> None:
        h = self.boot(COLD)
        self.read(h)
        self.sim._vf[(int(Domain.RING), -1, 0)] = VfWord.from_u32(0x27)
        self.read(h)  # the read sees the new word: the intent is built from it, so the plan is clean
        plan = self.plan(h, {"ring": {"points": {"4": -25}}})
        self.assertEqual([w for w in plan["warnings"] if w["code"] == "RING_NOT_RESTORED"], [])
        self.assertEqual(plan["units"], ["ring"])
        self.assertEqual(self.apply_ok(h, plan)["ring_refused"], [])
        self.assertEqual(self.sim.vf(0, int(Domain.RING), 4).offset_raw, -26)


class RingStale(RingIntentCase):
    def test_a_ring_word_that_changed_since_the_plan_makes_it_stale_before_any_write(self) -> None:
        h = self.boot(COLD)
        self.read(h)
        plan = self.plan(h, {"ring": {"points": {"4": -25}}})
        self.sim._vf[(int(Domain.RING), -1, 4)] = self.sim.vf(0, int(Domain.RING), 4).with_(offset_raw=-3)  # somebody else moved the ring point (test double; no mailbox write)
        start = len(self.sim.requests)
        r = self.apply(h, plan)
        self.assertEqual(error_code(r), "PLAN_STALE")
        self.assertIn("ring point 4", r["error"]["message"])
        self.assertEqual(ring_requests(self.sim, 0x11, start), [], "nothing was written")
        self.assertEqual(self.sim.vf(0, int(Domain.RING), 4).offset_raw, -3)

    def test_a_p_core_with_the_same_point_number_does_not_collide_with_the_ring(self) -> None:
        h = self.boot(COLD)
        self.read(h)
        plan = self.plan(h, {**rows(row("P0", curve=-10)), "ring": {"points": {"4": -25}}})
        self.apply_ok(h, plan)
        self.assertEqual(self.sim.vf(0, int(Domain.RING), 4).offset_raw, -26)


class NoStepLimit(RingIntentCase):
    def test_a_ring_intent_is_one_round_whatever_its_distance_and_warns_when_large(self) -> None:
        """D-049: no step limit -- a ring point and a core point move in one apply, in one round; a change over 50 mV only warns (LARGE_CHANGE)."""
        h = self.boot(COLD)
        self.read(h)
        core_plan = self.plan(h, rows(row("P0", points={"53": -75})))
        ring_plan = self.plan(h, {"ring": {"points": {"4": -75}}})
        for plan in (core_plan, ring_plan):
            self.assertEqual(plan["rounds"], 1)
            self.assertIn("LARGE_CHANGE", [w["code"] for w in plan["warnings"]])
        self.apply_ok(h, ring_plan)
        self.assertEqual(self.sim.vf(0, int(Domain.RING), 4).offset_raw, -77)
        self.apply_ok(h, core_plan)
