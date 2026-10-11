"""G-114: the pure V/F model (SPEC-v3 section 9 F4/F5/F6). No I/O."""
from __future__ import annotations

import unittest

from arltuner import vfmodel
from arltuner.vfmodel import check_turbo_order, complete_baseline, effective_curve, interpolate, predict_vccia, voltage_at

P_RATIOS = (8, 20, 30, 48, 53, 55, 56, 57)
# P0, measured stock [MEASURED lnx 2026-10-11] (tuning-results.md); 57x 1.4288 V (orchestrator single-core samples)
P0 = {8: 0.693, 20: 0.721, 30: 0.811, 48: 1.041, 53: 1.216, 55: 1.299, 56: 1.386, 57: 1.4288}


class ClampTests(unittest.TestCase):
    def test_the_measured_p0_57x_case_is_clamped_to_the_56x_voltage(self) -> None:
        """[MEASURED lnx 2026-10-02] P0 57x with -49.8 mV landed at -42.5 mV, i.e. at V(56x): 1.4288 - 0.0498 = 1.379 < 1.386."""
        curve = effective_curve(P_RATIOS, P0, {57: -49.8})
        top = curve[-1]
        self.assertAlmostEqual(top.effective_v, 1.386, places=6)
        self.assertEqual(top.clamped_by, 7)  # point 7 = 56x
        self.assertAlmostEqual((top.effective_v - top.base_v) * 1000, -42.8, places=1)  # ~ the measured -42.5 mV
        self.assertTrue(all(p.clamped_by is None for p in curve[:-1]))

    def test_no_clamp_when_the_offset_stays_above_the_previous_point(self) -> None:
        curve = effective_curve(P_RATIOS, P0, {57: -20})
        self.assertAlmostEqual(curve[-1].effective_v, 1.4088, places=6)
        self.assertIsNone(curve[-1].clamped_by)

    def test_a_raised_point_clamps_the_ones_above_it_and_the_chain_holds(self) -> None:
        curve = effective_curve(P_RATIOS, P0, {53: +120})  # 53x now 1.336 V: 55x (1.299) and 56x (1.386 -> no) ...
        by = {p.ratio: p for p in curve}
        self.assertAlmostEqual(by[53].effective_v, 1.336, places=6)
        self.assertAlmostEqual(by[55].effective_v, 1.336, places=6)
        self.assertEqual(by[55].clamped_by, 5)
        self.assertAlmostEqual(by[56].effective_v, 1.386, places=6)  # above 1.336 already
        self.assertIsNone(by[56].clamped_by)

    def test_a_point_without_a_baseline_is_left_out_of_the_chain(self) -> None:
        base = dict(P0)
        base[56] = None
        curve = effective_curve(P_RATIOS, base, {})
        self.assertIsNone(curve[6].effective_v)
        self.assertAlmostEqual(curve[7].effective_v, 1.4288, places=6)

    def test_complete_baseline_uses_the_fused_top_and_interpolates(self) -> None:
        base = complete_baseline(P_RATIOS, {8: 0.69, 20: 0.72, 30: 0.81, 48: 1.04, 53: 1.22, 55: 1.31}, fused_top_v=1.3047)
        self.assertAlmostEqual(base[57], 1.3047, places=6)  # not measured: the 0x09 voltage (F7)
        self.assertAlmostEqual(base[56], (1.31 + 1.3047) / 2, places=6)


class InterpolationTests(unittest.TestCase):
    def test_54x_is_the_midpoint_of_53x_and_55x(self) -> None:
        curve = effective_curve(P_RATIOS, P0, {})
        self.assertAlmostEqual(voltage_at(curve, 54), (1.216 + 1.299) / 2, places=6)

    def test_flat_above_the_top_point_follows_the_offset(self) -> None:
        """E 47-48x run on point 6 (46x): an offset at the top point moves the bins above it [MEASURED lnx 2026-10-02]."""
        e = (8, 15, 23, 30, 40, 46)
        base = {8: 0.775, 15: 0.775, 23: 0.775, 30: 0.775, 40: 0.970, 46: 1.157}
        self.assertAlmostEqual(voltage_at(effective_curve(e, base, {46: -35}), 48), 1.122, places=6)

    def test_an_oc_point_interpolates_from_the_top_point(self) -> None:
        curve = effective_curve(P_RATIOS, P0, {})
        self.assertAlmostEqual(voltage_at(curve, 58, oc=(59, 1.5)), (1.4288 + 1.5) / 2, places=6)
        self.assertAlmostEqual(voltage_at(curve, 57, oc=(59, 1.5)), 1.4288, places=6)

    def test_the_oc_point_of_ec1_matches_the_first_write_sittings(self) -> None:
        """G-120 fix 3 [MEASURED lnx 2026-10-11] (tuning-results.md "First write sittings"): E point 6 (46x) 1.1375 V."""
        e = (8, 15, 23, 30, 40, 46)
        curve = effective_curve(e, {8: 0.775, 15: 0.775, 23: 0.775, 30: 0.775, 40: 0.970, 46: 1.1375}, {})
        self.assertEqual(vfmodel.oc_point_voltage(curve, 1.117), (1.1375, True))  # 48x target below point 6: held up to it
        self.assertEqual(vfmodel.oc_point_voltage(curve, 1.147), (1.147, False))
        for ratio in (46, 47, 48):  # target 1.117 V: 48x = 47x = 1.1375 V
            self.assertAlmostEqual(voltage_at(curve, ratio, oc=(48, 1.117)), 1.1375, places=6)
        self.assertAlmostEqual(voltage_at(curve, 48, oc=(48, 1.147)), 1.147, places=6)  # measured 1.1475 (the target is quantised to 1/1024 V)
        self.assertAlmostEqual(voltage_at(curve, 47, oc=(48, 1.147)), (1.1375 + 1.147) / 2, places=6)  # 47x = the midpoint of 46x and 48x (measured 1.1425)
        self.assertAlmostEqual(voltage_at(curve, 48, oc=(48, 1.1367)), 1.1375, places=6)  # the neutral first write (1.1367 < 1.1375): unchanged
        self.assertAlmostEqual(voltage_at(curve, 50, oc=(48, 1.147)), 1.147, places=6)  # flat above the OC ratio
        self.assertIsNone(vfmodel.oc_point_voltage(effective_curve(e, {}, {}), 1.2))

    def test_interpolate_with_nothing_known(self) -> None:
        self.assertIsNone(interpolate({8: None}, 10))


class VcciaTests(unittest.TestCase):
    def test_the_highest_allowed_vid_or_the_ring_plus_105_mv(self) -> None:
        out = predict_vccia([("P7", 55, 1.376), ("P0", 55, 1.299), ("EC0", 48, 1.157)])
        self.assertEqual(out["set_by"], {"unit": "P7", "ratio": 55})
        self.assertAlmostEqual(out["predicted_v"], 1.481, places=4)

    def test_the_ring_is_the_floor(self) -> None:
        out = predict_vccia([("P0", 20, 0.8), ("EC0", 30, 0.775)])
        self.assertEqual(out["set_by"], {"unit": "ring", "ratio": 40})
        self.assertAlmostEqual(out["predicted_v"], vfmodel.RING_V + vfmodel.VCCIA_MARGIN_V, places=4)

    def test_nothing_known_is_none(self) -> None:
        self.assertIsNone(predict_vccia([("P0", 55, None)]))


class TurboOrderTests(unittest.TestCase):
    def test_non_increasing_is_fine(self) -> None:
        self.assertIsNone(check_turbo_order([57, 57, 55, 55, 55, 55, 55, 55]))
        self.assertIsNone(check_turbo_order([48] * 8))

    def test_a_rise_names_the_bucket(self) -> None:
        self.assertEqual(check_turbo_order([55, 57, 55, 55, 55, 55, 55, 55]), 1)
        self.assertEqual(check_turbo_order([57, 57, 55, 55, 55, 55, 55, 56]), 7)


if __name__ == "__main__":
    unittest.main()
