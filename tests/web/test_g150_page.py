"""G-150 on the Tune page: a boot-time OC ratio is named as one (E 48x above the fused 46x), and gets an offset control in the OC box.

The reference E state [MEASURED lnx 2026-10-13]: the fused curve ends at point 6 = 46x, the BIOS raised the max ratio to 48x (``0x1C`` 0x2E30, ``0x650`` 48x8) and there is no OC point (the P2=0 word
reads 0). Driven by the G-133 fixture with EC1's OC point removed, served by the fake helper; the intent is read from the ``tune.plan`` request. Static checks always run; the browser checks need
``ARLTUNER_BROWSER_TESTS=1`` (Playwright + Chrome). No hardware, no real helper code."""
from __future__ import annotations

import unittest

from tests.web import test_g133_page as g133

JS, VISIBLE, browser = g133.JS, g133.VISIBLE, g133.browser


def reference_e(result):
    """EC1 as EC0 / EC3 are: no OC point, 48x only through the max ratio and the E turbo table (the page reads ``tune``; ``units`` is kept in step)."""
    pts = result["tune"]["EC1"]["cpus"][0]["points"]
    pts[:] = [dict(p, ratio=None, v_target_v=None) if p["point"] == 0 else p for p in pts if not p.get("oc_point")]
    for u in result["units"]:
        if u["id"] == "EC1":
            c = u["cpus"][0]
            c["oc"] = dict(c["oc"], word="0x00000000", ratio=0, v_target_raw=0, v_target_v=0.0)
            c["points"] = [p for p in c["points"] if p["ratio"] != 48]


class TestStatic(unittest.TestCase):
    def test_wording_constants(self):
        self.assertIn('"No OC point"', JS)
        self.assertIn('": OC point ratio"', JS)
        self.assertIn("lowers the max from", JS)
        self.assertNotIn('"No OC ratio"', JS)
        self.assertNotIn("E has no 0x1C of its own", JS)
        self.assertNotIn("it has no 0x1C of its own", JS)


class TestBrowser(unittest.TestCase):
    session = g133.TestBrowser.session
    row = staticmethod(g133.TestBrowser.row)
    ratio_sel = g133.TestBrowser.ratio_sel
    points = g133.TestBrowser.points

    def off_sel(self, page, unit):
        return page.locator("#tn-pts-" + unit + " .tn-oc-off select")

    def intent(self, fake):
        return [r for r in fake.requests if r["op"] == "tune.plan"][-1]["args"]["intent"]

    @browser
    def test_the_header_names_the_boot_time_ratio_as_an_oc_ratio_and_point_6_carries_it(self):
        with self.session(mutate=reference_e) as (page, fake):
            ec0 = "offsets −35…0 mV · OC ratio: 48× (fused top 46×) · no OC point · runs up to 48×"
            self.assertEqual(self.row(page, "EC0").locator(".tn-now").inner_text(), ec0)
            self.assertEqual(self.row(page, "EC1").locator(".tn-now").inner_text(), ec0.replace("−35", "−25"))
            panel = self.points(page, "EC1")
            self.assertIn("Point 6 · 46× (also 47–48×)", panel.inner_text())
            self.assertIn("48× (OC ratio set at boot) · no OC point", panel.locator(".tn-oc").inner_text())
            self.assertIn("47–48× run on point 6’s voltage (no OC point)", panel.inner_text())
            self.assertIn("Point 6’s offset sets them", panel.inner_text())
            self.assertNotIn("No OC ratio configured", panel.inner_text())
            self.assertNotIn("turbo", (self.row(page, "EC1").inner_text() + panel.inner_text()).lower())

    @browser
    def test_the_ratio_dropdown_says_oc_point_and_a_lower_ratio_says_it_lowers_the_max(self):
        with self.session(mutate=reference_e) as (page, fake):
            sel = self.ratio_sel(page, "EC1")
            self.assertEqual(sel.get_attribute("aria-label"), "E-cluster 1: OC point ratio")
            self.assertEqual(sel.evaluate(VISIBLE), ["No OC point", "47×  · lowers the max from 48×", "48×", "49×"])
            self.assertEqual(self.ratio_sel(page, "P0").evaluate(VISIBLE), ["No OC point"])   # P-cores: nothing above their fused top is allowed here

    @browser
    def test_the_offset_dropdown_starts_at_point_6_and_offers_nothing_below_it(self):
        with self.session(mutate=reference_e) as (page, fake):
            self.points(page, "EC1")
            sel = self.off_sel(page, "EC1")
            texts = sel.evaluate(VISIBLE)
            self.assertEqual(texts[0], "same as point 6 (−25.4 mV) · no OC point")
            self.assertEqual(texts[1], "−25 mV")
            self.assertNotIn("−30 mV", texts)
            self.assertIn("−20 mV", texts)
            self.assertEqual(sel.evaluate("s => s.selectedOptions[0].textContent.trim()"), texts[0])
            self.assertEqual(self.points(page, "EC1").locator(".tn-oc-off .tn-oc-line").inner_text(), "48× offset")
            self.assertIn("Lower than point 6: lower point 6 (46–48× share its voltage).", self.points(page, "EC1").inner_text())
            self.assertEqual(sel.get_attribute("aria-label"), "E-cluster 1: 48× offset")
            self.assertTrue(sel.is_visible())
            self.assertFalse(sel.is_disabled())
            self.assertNotIn("An OC point stays until a cold boot", self.row(page, "EC1").inner_text())

    @browser
    def test_picking_an_offset_drafts_the_oc_point_at_the_max_ratio_with_the_target_on_the_grid(self):
        with self.session(mutate=reference_e) as (page, fake):
            self.points(page, "EC1")
            self.off_sel(page, "EC1").select_option(label="−20 mV")
            # fused V of EC1 1.1367 - 20 mV = 1.1167 -> the 5 mV target grid -> 1.115
            self.assertEqual(self.ratio_sel(page, "EC1").input_value(), "48")
            self.assertEqual(self.row(page, "EC1").locator("select.tn-sel").nth(2).input_value(), "1.115")   # the absolute target dropdown shows the same draft
            self.assertEqual(self.off_sel(page, "EC1").evaluate("s => s.selectedOptions[0].textContent.trim()"), "−20 mV")
            self.assertIn("An OC point stays until a cold boot; Restore accepted then reports it EQUIVALENT (G-147).", self.row(page, "EC1").inner_text())
            self.assertIn("Point 7 · OC", self.points(page, "EC1").inner_text())
            self.assertNotIn("(also 47", self.points(page, "EC1").inner_text())   # the OC point now gives 48x its own voltage
            page.click("#btn-tn-plan")
            page.wait_for_selector("#dlg-plan[open]")
            rows = self.intent(fake)["rows"]
            self.assertEqual([(r["target"], r["ratio"], r["v_target_v"]) for r in rows], [("EC1", 48, 1.115)])
            self.assertIsNone(rows[0]["offset"])

    @browser
    def test_going_back_to_point_6_drops_the_draft_and_a_raised_point_6_warns(self):
        with self.session(mutate=reference_e) as (page, fake):
            self.points(page, "EC1")
            self.off_sel(page, "EC1").select_option(label="−20 mV")
            self.off_sel(page, "EC1").select_option(index=0)
            self.assertEqual(self.ratio_sel(page, "EC1").evaluate("s => s.selectedOptions[0].textContent.trim()"), "No OC point")   # the draft is gone
            self.assertTrue(page.locator("#btn-tn-plan").is_disabled())    # no draft left
            self.assertNotIn("An OC point stays", self.row(page, "EC1").inner_text())
            self.off_sel(page, "EC1").select_option(label="−20 mV")
            self.assertNotIn("is raised to point 6", self.row(page, "EC1").inner_text())
            page.locator("#tn-pts-EC1 .tn-pt select").nth(5).select_option("0")   # point 6 above the 48x target
            self.assertIn("48× is raised to point 6’s voltage by the CPU.", self.row(page, "EC1").inner_text())

    @browser
    def test_an_oc_point_that_is_read_is_the_current_value_and_cannot_be_removed(self):
        with self.session() as (page, fake):   # EC1 as recorded: a real OC point at 48x with the fused top voltage as its target
            self.assertEqual(self.row(page, "EC1").locator(".tn-now").inner_text().split(" · ")[1:3], ["OC ratio: 48×", "OC point 7"])
            self.points(page, "EC1")
            sel = self.off_sel(page, "EC1")
            self.assertEqual(sel.evaluate("s => s.selectedOptions[0].textContent.trim()"), "0 mV  · now")
            self.assertNotIn("no OC point", sel.evaluate(VISIBLE)[0])
            sel.select_option(index=0)   # point 6's voltage: the target moves there, the OC point stays
            self.assertEqual(self.ratio_sel(page, "EC1").evaluate("s => s.selectedOptions[0].textContent.trim()"), "48×  · now")
            self.assertEqual(self.row(page, "EC1").locator("select.tn-sel").nth(2).input_value(), "1.11")
            self.assertNotIn("An OC point stays", self.row(page, "EC1").inner_text())   # it was already there

    @browser
    def test_a_group_row_gives_every_cluster_its_own_target(self):
        with self.session(mutate=reference_e) as (page, fake):
            page.click("#tn-scope button:has-text('All-Core')")
            e = page.locator(".tn-rowwrap").nth(1)
            e.locator(".seg button:has-text('Per V/F point')").click()
            sel = e.locator(".tn-oc-off select")
            self.assertEqual(sel.evaluate(VISIBLE)[0], "same as point 6 (mixed) · no OC point")   # EC0 −35.2, the others −25.4
            self.assertEqual(sel.evaluate(VISIBLE)[1], "−25 mV")   # above the highest point-6 offset
            sel.select_option(label="−20 mV")
            page.click("#btn-tn-plan")
            page.wait_for_selector("#dlg-plan[open]")
            rows = {r["target"]: r for r in self.intent(fake)["rows"]}
            self.assertEqual({u: (r["ratio"], r["v_target_v"]) for u, r in rows.items()}, {"EC0": (48, 1.135), "EC1": (48, 1.115), "EC2": (48, 1.115), "EC3": (48, 1.115)})   # fused V - 20 mV, per cluster

    @browser
    def test_the_highest_vid_prediction_uses_the_oc_point_target(self):
        with self.session(mutate=reference_e) as (page, fake):
            e_vid = lambda: float(page.locator("#tn-vccia-list li").nth(1).locator(".vc-val").inner_text().split()[0])   # noqa: E731
            page.wait_for_selector("#tn-vccia-list li")
            before = e_vid()
            self.assertLess(before, 1.2)   # point 6 (fused 1.1367 V on the baseline) carries 48x
            self.points(page, "EC1")
            self.off_sel(page, "EC1").select_option(label="+100 mV")   # target 1.1367 + 0.100 -> 1.235 V
            self.assertAlmostEqual(e_vid(), 1.235, places=3)

    @browser
    def test_p_cores_at_their_fused_top_have_no_offset_control_and_no_new_wording(self):
        with self.session(mutate=reference_e) as (page, fake):
            for unit in ("P0", "P1"):
                panel = self.points(page, unit)
                self.assertTrue(self.off_sel(page, unit).evaluate("s => s.closest('.tn-oc-off').hidden"), unit)
                self.assertIn("No OC ratio configured", panel.locator(".tn-oc").inner_text(), unit)
            self.assertIn("OC ratio: none", self.row(page, "P0").locator(".tn-now").inner_text())


if __name__ == "__main__":
    unittest.main()
