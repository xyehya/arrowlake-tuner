"""G-133 on the Tune page: the OC ratio model (ratio first, the adaptive voltage only for an OC point), the helper's live limits instead of a literal, All-Core / Per-Core labels, the unit
header line, no turbo text inside the per-unit panels, interpolated predictions.

Driven by ``tests/web/fixtures/tune-last-285k-2026-10-12.json`` -- a real ``tune.last`` result of the reference machine [MEASURED lnx 2026-10-12] -- served by the fake helper as the
stored read. Static checks always run; the browser checks need ``ARLTUNER_BROWSER_TESTS=1`` (Playwright + Chrome). No hardware, no real helper code."""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

HERE = Path(__file__).resolve().parent
WEB = HERE.parents[1] / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")
FIXTURE = json.loads((HERE / "fixtures" / "tune-last-285k-2026-10-12.json").read_text(encoding="utf-8"))["result"]
CHROME = os.environ.get("ARLTUNER_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or ""
ROW = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "EC0": 8, "EC1": 9, "EC2": 10, "EC3": 11}   # the index of a unit's row in Per-Core mode
VISIBLE = "s => Array.from(s.options).filter(o => !o.hidden).map(o => o.textContent.trim())"


class TestStatic(unittest.TestCase):
    def test_the_fixture_is_the_live_read_the_task_names(self):
        by = {u["id"]: u["cpus"][0]["oc"] for u in FIXTURE["units"]}
        self.assertEqual((by["EC1"]["ratio"], by["EC2"]["ratio"], by["EC0"]["ratio"], by["P0"]["ratio"]), (48, 46, 0, 57))
        self.assertNotIn("limits", FIXTURE)   # recorded before G-133: the fake adds them

    def test_no_policy_literal_remains_in_the_page(self):
        self.assertNotRegex(JS, r"\bPOLICY\.")
        for literal in ("offsetMax: 200", "ratioMax: { P:", "vtMax: {"):
            self.assertNotIn(literal, JS)

    def test_labels(self):
        for text in ('label: "All-Core"', 'label: "Per-Core"', 'label: "Whole Curve Offset"', 'label: "Per V/F point Offsets"', '"OC ratio", "Adaptive voltage (OC point)"'):
            self.assertIn(text, JS, text)
        for old in ('label: "Simple"', 'label: "Per unit"', 'label: "Whole curve"', 'label: "Per point"'):
            self.assertNotIn(old, JS, old)
        self.assertIn('id="tn-limits-note"', HTML)

    def test_the_turbo_text_is_not_built_into_the_unit_panels(self):
        self.assertNotIn("function ratioHint", JS)   # the "above the top turbo bucket" text of the ratio cell
        self.assertNotIn("oc-fused", JS)             # the "fused top point (0x09)" line of the OC box moved into its tooltip
        self.assertIn("eTurboNote(unitsOfKind(\"E\"))", JS)   # ... and the E turbo note is used by the turbo section only
        self.assertNotIn("eTurboNote(def.units)", JS)


def browser(test):
    return unittest.skipUnless(os.environ.get("ARLTUNER_BROWSER_TESTS") == "1" and sync_playwright and CHROME, "set ARLTUNER_BROWSER_TESTS=1 (needs playwright + chrome)")(test)


class TestBrowser(unittest.TestCase):
    @contextlib.contextmanager
    def session(self, *, ratio_max=None, limits=True, fixture=True, seed=True, interpolate=True, offset_window=None, hw_limits=None):
        from tests.web import v3_harness as h

        srv, fake, stop = h.start(seed=False)
        fake.limits_enabled = limits
        fake.offset_window, fake.hw_limits = offset_window, hw_limits
        fake.interpolate = interpolate
        if ratio_max:
            fake.ratio_max = dict(ratio_max)
        if fixture:
            fake.fixture_result = json.loads(json.dumps(FIXTURE))
            fake.seed_last_read(time.time() - 120)
        if seed:
            self.baseline = fake.seed_curve("Stock baseline", baseline=True, offset_mv=0.0, created="2026-10-07T20:41:09Z", units=["P0", "P1", "EC0", "EC1"])
        self.addCleanup(fake.close)
        self.addCleanup(srv.close)
        self.addCleanup(stop.set)
        logs: list[tuple[str, str]] = []
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
            try:
                page = b.new_context(viewport={"width": 1400, "height": 1000}).new_page()
                page.on("console", lambda m: logs.append((m.type, m.text)))
                page.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
                h.login(page, srv)
                page.goto(srv.origin + "/")
                page.wait_for_selector("#conn-helper.s-ok", timeout=10000)
                page.click("#tab-tune")
                page.wait_for_selector("#tn-console:not([hidden])", timeout=10000)
                page.click("#tn-scope button:has-text('Per-Core')")
                yield page, fake
            finally:
                b.close()
        errors = [m for m in logs if m[0] in ("error", "pageerror", "warning") and "favicon" not in m[1] and "404" not in m[1]]
        self.assertEqual(errors, [])

    @staticmethod
    def row(page, unit):
        return page.locator(".tn-rowwrap").nth(ROW[unit])

    def ratio_sel(self, page, unit):
        return self.row(page, unit).locator("select.tn-sel").nth(1)

    def vt_sel(self, page, unit):
        return self.row(page, unit).locator("select.tn-sel").nth(2)

    def points(self, page, unit):
        self.row(page, unit).locator(".seg button:has-text('Per V/F point')").click()
        return page.locator("#tn-pts-" + unit)

    # ---- the ratio dropdown ranges against the live ratio max -------------------------------------------------------------------------------------------------
    @browser
    def test_with_a_live_ratio_max_of_57_the_favored_cores_cannot_choose_and_say_why(self):
        with self.session() as (page, fake):
            for unit in ("P0", "P3"):   # stock 57x = the policy's 57x: nothing above it is allowed
                sel = self.ratio_sel(page, unit)
                self.assertTrue(sel.is_disabled(), unit)
                self.assertEqual(sel.evaluate(VISIBLE), ["No OC ratio"], unit)
                self.assertIn("OC ratios above 57× are not allowed by your policy (/etc/arltuner/policy.toml ratio_max)", self.row(page, unit).inner_text())
            sel = self.ratio_sel(page, "P1")   # stock 55x: 56x and 57x are the middle band (fused points 7 and 8, offset only)
            self.assertFalse(sel.is_disabled())
            self.assertEqual(sel.evaluate(VISIBLE), ["No OC ratio", "56×", "57×"])
            self.assertNotIn("not allowed by your policy", self.row(page, "P1").inner_text())

    @browser
    def test_with_a_test_policy_max_of_60_the_ratios_above_the_fused_top_open_up(self):
        with self.session(ratio_max={"P": 60, "E": 49}) as (page, fake):
            for unit in ("P0", "P3"):
                sel = self.ratio_sel(page, unit)
                self.assertFalse(sel.is_disabled(), unit)
                self.assertEqual(sel.evaluate(VISIBLE), ["No OC ratio", "58×", "59×", "60×"], unit)
            self.assertEqual(self.ratio_sel(page, "P1").evaluate(VISIBLE), ["No OC ratio", "56×", "57×", "58×", "59×", "60×"])   # 56-57x: the middle band, on the non-favored core only

    @browser
    def test_the_e_clusters_offer_47_to_the_live_max_and_an_oc_ratio_cannot_be_cleared(self):
        with self.session() as (page, fake):
            self.assertEqual(self.ratio_sel(page, "EC0").evaluate(VISIBLE), ["No OC ratio", "47×", "48×", "49×"])
            ec1 = self.ratio_sel(page, "EC1")   # holds 48x: "No OC ratio" is shown but cannot be chosen
            self.assertEqual(ec1.evaluate(VISIBLE), ["No OC ratio", "47×", "48×  · now", "49×"])
            self.assertTrue(ec1.locator("option[value=__none]").is_disabled())
            self.assertIn("cannot be cleared at runtime", ec1.locator("option[value=__none]").get_attribute("title"))
            self.assertEqual(ec1.input_value(), "48")
            self.assertEqual(self.ratio_sel(page, "EC0").input_value(), "__none")

    # ---- the adaptive voltage belongs to the OC point ---------------------------------------------------------------------------------------------------------
    @browser
    def test_the_adaptive_voltage_is_disabled_without_an_oc_ratio_and_enabled_for_an_oc_point(self):
        with self.session() as (page, fake):
            for unit in ("P0", "P1", "EC0", "EC2"):
                vt = self.vt_sel(page, unit)
                self.assertTrue(vt.is_disabled(), unit)
                self.assertTrue(vt.evaluate("s => s.parentElement.title").startswith("set an OC ratio first: the adaptive voltage belongs to the OC point"), unit)
            self.assertFalse(self.vt_sel(page, "EC1").is_disabled())   # EC1: OC point 7, 48x + 1.1367 V
            self.assertIn("1.1367 V", self.vt_sel(page, "EC1").evaluate("s => s.selectedOptions[0].textContent"))

    @browser
    def test_each_oc_state_reads_in_the_row(self):
        with self.session(ratio_max={"P": 60, "E": 49}) as (page, fake):
            self.assertIn("No OC ratio configured", self.row(page, "P0").inner_text())    # word 57x = its stock ratio
            self.assertIn("No OC ratio configured", self.row(page, "P1").inner_text())    # word 0
            self.assertIn("OC point 7 · 48× · adaptive V", self.row(page, "EC1").inner_text())
            self.assertIn("No OC ratio configured", self.row(page, "EC2").inner_text())   # word 46x + 1.1367 V = the fused top: no OC point
            self.assertNotIn("inactive", self.row(page, "EC2").inner_text())              # the retained word is shown in the raw view only
            page.click("#btn-tn-hex")
            self.assertIn("(word holds 46× + 1.137 V, inactive)", self.row(page, "EC2").inner_text())
            self.assertIn("(word holds 57×, inactive)", self.row(page, "P0").inner_text())
            self.assertNotIn("inactive", self.row(page, "EC1").inner_text())
            # a non-favored core given 56x: it lands on its fused point 7, offset only, no adaptive voltage
            self.ratio_sel(page, "P1").select_option("56")
            self.assertIn("OC ratio 56× · uses fused point 7 (offset only)", self.row(page, "P1").inner_text())
            self.assertTrue(self.vt_sel(page, "P1").is_disabled())

    @browser
    def test_an_oc_point_without_a_target_is_prefilled_with_the_fused_top_voltage_and_taken_back_with_the_ratio(self):
        with self.session(ratio_max={"P": 60, "E": 49}) as (page, fake):
            self.ratio_sel(page, "P0").select_option("58")
            vt = self.vt_sel(page, "P0")
            self.assertFalse(vt.is_disabled())
            self.assertEqual(vt.input_value(), "1.425")   # P0's fused top 1.4258 V on the 5 mV grid
            row = self.row(page, "P0")
            self.assertIn("writes 1.4248 V", row.inner_text())   # 1.425 V as the hardware keeps it (1/1024 V)
            self.assertIn("the fused top point’s voltage (0x09), nothing was set", row.inner_text())
            self.assertIn("changed", row.get_attribute("class"))
            self.assertIn("OC point 9 · 58× · adaptive V", row.inner_text())
            self.ratio_sel(page, "P0").select_option("__none")   # P0 holds no OC ratio, so "No OC ratio" can be chosen again
            self.assertTrue(vt.is_disabled())
            self.assertNotIn("changed", row.get_attribute("class") or "")
            self.assertNotIn("writes", row.inner_text())
            # EC2 retains its target in the word: an OC ratio keeps it, nothing is prefilled over it
            self.ratio_sel(page, "EC2").select_option("48")
            self.assertEqual(self.vt_sel(page, "EC2").input_value(), "1.1367")
            self.assertNotIn("nothing was set", self.row(page, "EC2").inner_text())

    @browser
    def test_an_all_core_row_follows_the_same_rules_over_the_group(self):
        with self.session() as (page, fake):
            page.click("#tn-scope button:has-text('All-Core')")
            rows = page.locator(".tn-rowwrap")
            p, e = rows.nth(0), rows.nth(1)
            self.assertEqual(p.locator("select.tn-sel").nth(1).evaluate(VISIBLE), ["No OC ratio"])   # the group starts above its highest stock ratio (57x)
            self.assertTrue(p.locator("select.tn-sel").nth(1).is_disabled())
            self.assertIn("mixed", e.inner_text())   # EC1 has an OC point, the others none: shown, never chosen
            self.assertTrue(e.locator("select.tn-sel").nth(2).is_disabled())
            self.assertEqual(e.locator("select.tn-sel").nth(1).input_value(), "__mixed")

    # ---- the header line, the panels ----------------------------------------------------------------------------------------------------------------------------
    @browser
    def test_the_unit_header_says_offsets_oc_ratio_and_how_far_it_runs(self):
        with self.session() as (page, fake):
            ec0 = self.row(page, "EC0").locator(".tn-now")
            self.assertEqual(ec0.inner_text(), "offsets −35…0 mV · OC ratio: none · runs up to 48×")
            self.assertIn("0x650", ec0.get_attribute("title"))
            self.assertEqual(self.row(page, "EC1").locator(".tn-now").inner_text(), "offsets −25…0 mV · OC ratio: 48× · runs up to 48×")
            self.assertEqual(self.row(page, "EC2").locator(".tn-now").inner_text(), "offsets −25…0 mV · OC ratio: none · runs up to 48×")   # the retained 46x word does nothing
            self.assertIn("runs up to 57×", self.row(page, "P0").locator(".tn-now").inner_text())
            p1 = self.row(page, "P1").locator(".tn-now").inner_text()
            self.assertEqual(p1, "offsets −50…0 mV · OC ratio: none · runs up to 55×")   # the table says 57x, the core's own 0x1C ratio 55x
            for unit in ROW:
                text = self.row(page, unit).locator(".tn-now").inner_text()
                for old in ("target n/a", "ratio n/a", "fused top"):
                    self.assertNotIn(old, text)

    @browser
    def test_no_turbo_text_in_the_per_unit_panels_and_one_vf_fact_for_the_e_clusters(self):
        with self.session() as (page, fake):
            for unit in ("P0", "P1", "EC0", "EC1"):
                panel = self.points(page, unit)
                text = (self.row(page, unit).inner_text() + panel.inner_text()).lower()
                self.assertNotIn("turbo", text, unit)
                self.assertNotIn("fused top point (0x09)", text, unit)
            self.assertIn("47–48× run on point 6’s voltage (no OC point)", page.locator("#tn-pts-EC0").inner_text())
            self.assertNotIn("run on point", page.locator("#tn-pts-EC1").inner_text())   # EC1 has an OC point
            self.assertIn("Fused top point (0x09): 46×, 1.1367 V", page.locator("#tn-pts-EC1 .tn-oc").get_attribute("title"))   # moved into the OC box tooltip
            self.assertIn("Point 7 · OC", page.locator("#tn-pts-EC1 .tn-oc").inner_text())
            self.assertIn("E turbo table: up to 48×", page.inner_text("#tn-turbo"))   # the turbo text stays in the turbo section

    @browser
    def test_points_above_a_core_s_own_ratio_are_tagged_not_predicted(self):
        with self.session() as (page, fake):
            p1 = self.points(page, "P1")
            over = p1.locator(".tn-pt.unreach")
            self.assertEqual(over.count(), 2)
            for i, ratio in enumerate((56, 57)):
                text = over.nth(i).inner_text()
                self.assertIn(f"{ratio}×", text)
                self.assertIn("above 55× limit", text)
                self.assertNotIn("→", text)
                self.assertFalse(over.nth(i).locator("select").is_disabled())   # still editable
            self.assertEqual(self.points(page, "P0").locator(".tn-pt.unreach").count(), 0)

    # ---- labels -----------------------------------------------------------------------------------------------------------------------------------------------------------
    @browser
    def test_the_new_labels(self):
        with self.session() as (page, fake):
            self.assertEqual(page.locator("#tn-scope button").all_inner_texts(), ["All-Core", "Per-Core"])
            self.assertEqual(self.row(page, "P0").locator(".seg button").all_inner_texts(), ["Whole Curve Offset", "Per V/F point Offsets"])
            self.assertEqual(page.locator(".tn-head .tn-col").all_inner_texts()[:4], ["Unit", "Voltage offset", "OC ratio", "Adaptive voltage (OC point)"])
            self.assertIn("Per-Core:", page.inner_text("#tn-scope-note"))
            self.assertEqual(self.row(page, "P0").locator("select.tn-sel").first.get_attribute("aria-label"), "P-core 0: Whole Curve Offset")
            text = page.inner_text("#tn-console")
            for old in ("Simple", "Per unit", "Whole curve", "Per point"):
                self.assertNotIn(old, text, old)

    # ---- no limits from the helper ---------------------------------------------------------------------------------------------------------------------------------------
    @browser
    def test_a_helper_without_limits_leaves_every_value_control_disabled_with_one_reason(self):
        with self.session(limits=False) as (page, fake):
            note = page.inner_text("#tn-limits-note")
            self.assertIn("did not report the policy limits", note)
            row = self.row(page, "EC1")
            for i in range(3):
                self.assertTrue(row.locator("select.tn-sel").nth(i).is_disabled(), i)
            self.points(page, "EC1")
            self.assertTrue(page.locator("#tn-pts-EC1 .tn-pt select").first.is_disabled())
            self.assertTrue(page.locator("#tn-turbo select").first.is_disabled())
            self.assertIn("update the helper", note)

    @browser
    def test_the_limits_decide_the_offset_window_and_the_turbo_ceiling(self):
        with self.session() as (page, fake):
            opts = self.row(page, "P1").locator("select.tn-sel").first.evaluate("s => Array.from(s.options).filter(o => !o.hidden).map(o => o.value)")
            self.assertEqual((opts[0], opts[-1]), ("-200", "200"))
        from tests.web import fake_v3

        orig = fake_v3.OFFSET_LIMIT
        fake_v3.OFFSET_LIMIT = 50
        try:
            with self.session(ratio_max={"P": 59, "E": 49}) as (page, fake):
                opts = self.row(page, "P1").locator("select.tn-sel").first.evaluate("s => Array.from(s.options).filter(o => !o.hidden && !o.disabled).map(o => o.value)")
                self.assertEqual((opts[0], opts[-1]), ("-50", "50"))
                top = page.locator("#tn-turbo .turbo-kind.k-P select").first.evaluate("s => Array.from(s.options).map(o => o.value)")
                self.assertEqual(top[-1], "59")   # bucket 0 of the P table may rise to the live ratio max
        finally:
            fake_v3.OFFSET_LIMIT = orig

    # ---- D-039: every limit says it is the user's policy, and the dropdowns follow it with no client cap ----------------------------------------------------------
    @browser
    def test_a_policy_wider_than_200_mv_is_offered_in_full_and_says_where_it_comes_from(self):
        hw = {"P": {"ratio_max_oc": 62, "v_target_max_mv": 1625, "offset_mv": [-1000, 999]}, "E": {"ratio_max_oc": 60, "v_target_max_mv": 1355, "offset_mv": [-1000, 999]}}
        with self.session(ratio_max={"P": 60, "E": 49}, offset_window=(-500, 300), hw_limits=hw) as (page, fake):
            opts = "s => Array.from(s.options).filter(o => !o.hidden).map(o => o.value)"
            vals = self.row(page, "P1").locator("select.tn-sel").first.evaluate(opts)
            self.assertEqual((vals[0], vals[1], vals[-1]), ("-500", "-495", "300"))   # the 5 mV step stays, the window is the policy's
            pvals = self.points(page, "P1").locator("select").first.evaluate(opts)
            self.assertEqual((pvals[0], pvals[-1]), ("-500", "300"))
            off_tip = self.row(page, "P1").locator(".tn-cell").first.get_attribute("title")
            self.assertEqual(off_tip, "Limit: −500…+300 mV from your policy (/etc/arltuner/policy.toml) — editable; your CPU allows −1000…+999 mV.")
            self.assertIn("Limit: up to 1.355 V from your policy (/etc/arltuner/policy.toml) — editable; your CPU allows up to 1.355 V.", self.vt_sel(page, "EC1").evaluate("s => s.parentElement.title"))
            self.assertIn("Limit: up to 49× from your policy (/etc/arltuner/policy.toml) — editable; your CPU allows OC ratios up to 60×.", self.ratio_sel(page, "EC1").evaluate("s => s.parentElement.title"))
            self.assertIn("Limit: up to 60× from your policy", page.locator("#tn-turbo .turbo-kind.k-P select").first.get_attribute("title"))
            self.assertIn("Limits are your policy, not the tool’s: edit /etc/arltuner/policy.toml to go further (hard ceiling = what your CPU reports).", page.inner_text("#tn-limits-note"))

    @browser
    def test_without_hw_limits_the_tooltip_only_says_editable(self):
        with self.session() as (page, fake):
            tip = self.row(page, "P1").locator(".tn-cell").first.get_attribute("title")
            self.assertEqual(tip, "Limit: −200…+200 mV from your policy (/etc/arltuner/policy.toml) — editable in /etc/arltuner/policy.toml.")
            self.assertNotIn("your CPU allows", self.ratio_sel(page, "P1").evaluate("s => s.parentElement.title"))
            self.assertIn("Limits are your policy, not the tool’s", page.inner_text("#tn-limits-note"))

    # ---- interpolated predictions ------------------------------------------------------------------------------------------------------------------------------------------
    @browser
    def test_a_point_the_baseline_could_not_measure_is_interpolated_between_its_neighbours(self):
        with self.session(interpolate=False) as (page, fake):   # a baseline made before G-132: E 23x is unreachable (cap granularity)
            e = {p["ratio"]: p for p in self.baseline["units"]["EC0"]["points"]}
            self.assertEqual((e[23]["status"], e[15]["status"], e[30]["status"]), ("unreachable", "ok", "ok"))
            panel = self.points(page, "EC0")
            cell = panel.locator(".tn-pt", has_text="Point 3 · 23×")
            text = cell.inner_text()
            self.assertIn("(interpolated)", text)
            self.assertNotIn("no measured voltage", text)
            want = e[15]["vid_v"] + (e[30]["vid_v"] - e[15]["vid_v"]) * (23 - 15) / (30 - 15)   # the unit's offset there is 0 in the fixture
            got = float(re.search(r"→ ([\d.]+) V", text).group(1))
            self.assertAlmostEqual(got, want, delta=0.0006)
            self.assertIn("interpolated between the neighbouring measured points", cell.locator(".tn-pt-eff").get_attribute("title"))
            self.assertNotIn("(interpolated)", panel.locator(".tn-pt", has_text="Point 2 · 15×").inner_text())   # measured points are plain


if __name__ == "__main__":
    unittest.main()
