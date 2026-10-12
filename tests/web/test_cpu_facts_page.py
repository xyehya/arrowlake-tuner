"""G-115: the page shows and enforces the CPU facts of SPEC-v3 section 9 (F1-F9) on Tune and Curves, bound to the contract 9 addendum.

Static checks on the three files (always run) plus an opt-in browser check (``ARLTUNER_BROWSER_TESTS=1``, Playwright + Chrome) against the fake helper.
No hardware, no real helper code."""
from __future__ import annotations

import os
import re
import shutil
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

WEB = Path(__file__).resolve().parents[2] / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")
CSS = (WEB / "style.css").read_text(encoding="utf-8")
CHROME = os.environ.get("ARLTUNER_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or ""


def body_of_const(name: str) -> str:
    m = re.search(r"const " + re.escape(name) + r" = \{(.*?)\n  \};", JS, re.S)
    assert m, name
    return m.group(1)


def body_of(name: str) -> str:
    m = re.search(r"function " + re.escape(name) + r"\(.*?\) \{(.*?)\n  \}\n", JS, re.S)
    assert m, name
    return m.group(1)


class TestStatic(unittest.TestCase):
    def test_addendum_fields_are_bound(self):
        for name in ("effective_v", "clamped_by", "vccia", "predicted_v", "set_by", "fused", "turbo", "favored", "counts", "reason"):
            self.assertIn(name, JS, name)
        for reason in ("max_turbo", "cap_granularity", "cpufreq_granularity", "active_cores", "turbo_off", "user_cap"):
            self.assertIn(reason, JS, reason)
        self.assertIn('chk === "TURBO_ORDER"', JS)

    def test_a_helper_without_intent_turbo_is_named_not_hidden(self):
        """The real canonical_intent accepts exactly {scope, rows}; a BAD_REQUEST on a plan carrying intent.turbo says so instead of a generic error."""
        body = body_of("requestPlan")
        self.assertIn("intent.turbo !== undefined", body)
        self.assertIn("does not accept turbo table edits (intent.turbo)", body)

    def test_plan_warnings_are_rendered_by_code_not_by_regex(self):
        """G-117: warnings[].code picks the look; the old text regexes are gone; the VccIA block shows label / margin_v / light_load."""
        for code in ("CLAMPED", "E40_OFFSET", "E_TARGET_WHEA", "POSITIVE_OFFSET", "NO_BASELINE", "TURBO_RAISE", "NOTE"):
            self.assertIn(code + ":", body_of_const("WARN_CODES"), code)
        self.assertNotIn("warnClass", JS)
        self.assertNotRegex(JS, r"/40\\s\*\[x")
        self.assertIn("w.code", body_of("planWarnings"))
        node = body_of("vcciaNode")
        for field in ("vc.label", "vc.margin_v", "vc.light_load", "vc.before_v", "vc.set_by"):
            self.assertIn(field, node, field)

    def test_a_turbo_only_edit_sends_turbo_alone(self):
        self.assertIn("unitsChanged().length ? buildIntent() : {}", JS)
        self.assertIn("(x) => isObj(x) && x.kind === \"write_msr\"", JS)   # the review says the plan is plain MSR writes only

    def test_grid_uses_the_community_point_names(self):
        self.assertIn('"Point " + (no === null ? "?" : no) + " \\u00b7 " + ratio', JS)   # Point 1 . 8x ... Point 8 . 57x
        self.assertIn('"Point " + ocPointNo(units) + " \\u00b7 OC"', JS)                  # Point 9 . OC (P) / Point 7 . OC (E)
        self.assertIn("fused \\u00b7 offset only", JS)
        self.assertIn("An OC ratio is a ratio above what the unit runs at stock. The OC point (ratio + adaptive voltage) exists once that ratio is above the top fused ratio", JS)
        self.assertIn("E turbo table: up to ", body_of("eTurboNote"))   # D-036: derived from the E turbo table that was read, not a fixed "47-48x"
        self.assertNotIn("47\\u201348", JS)

    def test_a_read_oc_point_is_the_oc_point_never_an_extra_fused_column(self):
        """G-125: a read point flagged oc_point (EC1 P2 = 7 = 48x) stays out of the fused point set the grid and the predictions are built from."""
        self.assertIn("p.oc_point !== true", body_of("curPts"))
        self.assertIn("oc_point: p.oc_point === true", JS)
        self.assertIn("if (p.point > fusedN) p.oc_point = true", JS)   # an older read without the flag: judged by the point number (P 9, E 7)
        self.assertIn('"Point " + ocPointNo(units) + " \\u00b7 OC"', JS)

    def test_the_before_after_table_names_the_unit_on_every_row(self):
        body = body_of("renderPlanBody")
        self.assertIn('td(d.unit, "plan-unit")', body)
        self.assertNotIn('i === 0 ? d.unit : ""', body)

    def test_the_assumed_words_line_follows_the_helper_flag_only(self):
        self.assertIn("res.words_assumed === true", body_of("renderPlanBody"))

    def test_reach_comes_from_the_read_never_from_a_hardcoded_core_list(self):
        body = body_of("reachOf")
        self.assertIn("turbo.favored", body)
        self.assertNotRegex(body, r"\bFAVORED\b")   # P0 / P3 are not named: they reach 57x because the read says so
        self.assertIn("above this core\\u2019s ", JS)   # G-133: the tag on a point above the core's own 0x1C ratio

    def test_clamp_is_the_monotonic_rule(self):
        self.assertIn("want < prev.v", body_of("predictUnit"))   # V(n) = max(base + offset, V(n-1))
        self.assertIn("clamped to Vn\\u22121", JS)

    def test_turbo_editor_never_offers_a_rising_bucket(self):
        self.assertIn("i === 0 ? Math.max(cur[0], eff[0], lim && isNum(lim.ratioMax) ? lim.ratioMax : 0) : eff[i - 1]", body_of("renderTurbo"))   # G-133: the live ratio max, no literal
        self.assertNotIn("POLICY.", JS)
        self.assertIn("Math.min(t[j], v)", body_of("setBucket"))

    def test_f8_warnings(self):
        self.assertIn("a positive offset raises the voltage", JS)
        self.assertIn("WHEA risk", JS)       # the label of the helper's E_TARGET_WHEA warning (shown as a community hint)
        self.assertNotIn("OCCT", JS)         # D-036: no client-side E 40x lore; the warning shows only when the helper sends the code
        self.assertIn(".tn-warn li.bad", CSS)

    def test_no_fixed_ring_or_margin_on_the_tune_page(self):
        """D-036: the Tune VccIA card is the highest predicted VID among the units; ring and headroom are not constants of the page."""
        self.assertNotIn("ring: 1.20", JS)
        self.assertNotIn("VCCIA", JS)
        self.assertNotIn("ring: 1.20, margin: 0.105", JS)
        self.assertIn("function vidFor", JS)
        self.assertIn('id="tn-vccia-live"', HTML)

    def test_new_ids_exist_and_csp_rules_hold(self):
        for i in ("tn-vccia-card", "tn-vccia-line", "tn-vccia-list", "tn-turbo-card", "tn-turbo", "cv-base-note", "cv-unreach"):
            self.assertIn(f'id="{i}"', HTML, i)
        self.assertNotRegex(JS, r"\.style\.")
        self.assertNotIn("innerHTML", JS)

    def test_g131_status_counts_notes_and_the_chart_has_a_not_measured_row(self):
        body = body_of("onMeasureResult")
        self.assertNotIn('"Problems: "', body)
        self.assertIn("see below", body)
        self.assertIn("vc-nm", JS)
        self.assertIn("function notMeasuredCaption", JS)

    def test_pins_are_svg_with_titles_built_via_dom(self):
        self.assertIn('sv("title")', JS)
        self.assertNotIn(".vc-pin.reach", CSS)   # G-131: unmeasured points are ticks in a row under the axis, not rings in the plot
        self.assertIn(".vc-nm line", CSS)
        self.assertNotIn('kind: "reach"', JS)
        self.assertIn(".vc-pin.fused path", CSS)


@unittest.skipUnless(os.environ.get("ARLTUNER_BROWSER_TESTS") == "1" and sync_playwright and CHROME, "set ARLTUNER_BROWSER_TESTS=1 (needs playwright + chrome)")
class TestBrowser(unittest.TestCase):
    def test_a_read_oc_point_renders_as_the_oc_point_only(self):
        """G-125: EC1 reads P2 = 7 = 48x flagged oc_point: Tune shows six fused points plus "Point 7 · OC", never a seventh "fused · offset only" column."""
        from tests.web import v3_harness as h

        srv, fake, stop = h.start(seed=True)
        fake.oc_point_units = {"EC1"}
        self.addCleanup(fake.close)
        self.addCleanup(srv.close)
        self.addCleanup(stop.set)
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
            try:
                page = b.new_context(viewport={"width": 1400, "height": 1000}).new_page()
                h.login(page, srv)
                page.goto(srv.origin + "/")
                page.wait_for_selector("#conn-helper.s-ok", timeout=10000)
                page.click("#tab-tune")
                page.wait_for_selector("#btn-tune-read:not([disabled])", timeout=10000)
                page.click("#btn-tune-read")
                page.wait_for_selector("#tn-console:not([hidden])", timeout=15000)
                page.click("#tn-scope button:has-text('Per-Core')")
                page.locator(".tn-rowwrap").nth(9).locator(".seg button:has-text('Per V/F point')").click()
                pts = page.locator("#tn-pts-EC1 .tn-pt")
                self.assertEqual(pts.count(), 7)   # 6 fused + the OC box
                self.assertEqual(page.locator("#tn-pts-EC1 .tn-pt-tag:has-text('fused')").count(), 6)
                self.assertIn("Point 7 · OC", page.locator("#tn-pts-EC1 .tn-oc").inner_text())
            finally:
                b.close()

    def test_facts_on_tune_and_curves(self):
        from tests.web import v3_harness as h

        srv, fake, stop = h.start(seed=True)
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
                page.wait_for_selector("#btn-tune-read:not([disabled])", timeout=10000)
                page.click("#btn-tune-read")
                page.wait_for_selector("#tn-console:not([hidden])", timeout=15000)
                page.click("#tn-scope button:has-text('Per-Core')")
                for idx in (0, 1, 9):   # P0, P1, EC1
                    page.locator(".tn-rowwrap").nth(idx).locator(".seg button:has-text('Per V/F point')").click()
                # F1 / F2: the unit's real points, then the OC point column
                p0 = page.locator("#tn-pts-P0 .tn-pt")
                self.assertEqual(p0.count(), 9)
                self.assertIn("Point 1 · 8×", p0.nth(0).inner_text())
                self.assertIn("fused · offset only", p0.nth(0).inner_text())
                self.assertIn("Point 9 · OC", p0.nth(8).inner_text())
                self.assertIn("No OC ratio configured", p0.nth(8).inner_text())   # G-133: P0 reads 57x, its stock ratio = the fused top: no OC ratio
                self.assertIn("No OC ratio configured", page.locator("#tn-pts-P1 .tn-oc").inner_text())
                self.assertIn("Point 7 · OC", page.locator("#tn-pts-EC1 .tn-oc").inner_text())
                self.assertIn("47–48× run on point 6’s voltage (no OC point)", page.locator("#tn-pts-EC1").inner_text())   # G-133: the one V/F fact; the E turbo text lives in the turbo section only
                self.assertNotIn("E turbo table", page.locator("#tn-pts-EC1").inner_text())
                # F3: 56x / 57x on P1 are dimmed; the same points on P0 (favored) are not
                self.assertEqual(page.locator("#tn-pts-P1 .tn-pt.unreach").count(), 2)
                self.assertEqual(page.locator("#tn-pts-P0 .tn-pt.unreach").count(), 0)
                self.assertIn("fused · offset only · above this core’s 55× limit", page.locator("#tn-pts-P1 .tn-pt.unreach").first.inner_text())
                for i in range(2):   # 56x and 57x alike: the tag only, never a predicted voltage
                    self.assertNotIn("→", page.locator("#tn-pts-P1 .tn-pt.unreach").nth(i).inner_text())
                # F4: a deep offset on 57x with a milder one on 56x is clamped; the badge shows
                self.assertEqual(page.locator("#tn-pts-P0 .tn-badge:visible").count(), 0)
                p0.nth(7).locator("select").select_option("-100")
                p0.nth(6).locator("select").select_option("-10")
                page.wait_for_selector("#tn-pts-P0 .tn-badge:visible")
                self.assertIn("clamped to Vn−1", p0.nth(7).inner_text())
                v56 = re.search(r"→ ([\d.]+) V", p0.nth(6).inner_text()).group(1)
                v57 = re.search(r"→ ([\d.]+) V", p0.nth(7).inner_text()).group(1)
                self.assertEqual(v56, v57)
                # F8: an E 40x offset is flagged by the helper's coded warning in the review, not by a client-side rule
                page.locator("#tn-pts-EC1 .tn-pt").nth(4).locator("select").select_option("-20")
                self.assertEqual(page.locator(".tn-warn li.bad").count(), 0)
                # F6 / F5: the card names the unit that sets the highest predicted VID; a turbo bucket cannot be raised above its left neighbour
                self.assertIn("Highest predicted VID ≈", page.inner_text("#tn-vccia-line"))
                sel = page.locator("#tn-turbo .turbo-kind").nth(0).locator("select")
                values = [int(o) for o in sel.nth(3).locator("option").evaluate_all("els => els.map(e => e.value)")]
                self.assertEqual(max(values), int(sel.nth(2).input_value()))
                sel.nth(1).select_option("53")   # lowering bucket 1 brings the right-hand buckets down with it
                self.assertEqual({sel.nth(i).input_value() for i in range(2, 8)}, {"53"})
                page.click("#btn-tn-plan")
                page.wait_for_selector("#dlg-plan[open]")
                dlg = page.inner_text("#dlg-plan")
                self.assertIn("Predicted VccIA", dlg)
                self.assertIn("clamped to Vn−1", dlg)
                self.assertIn("Turbo ratio tables", dlg)
                # G-117: the review badges warnings by code; the VccIA block carries the label, the headroom and the 1-2 core case
                self.assertEqual(page.locator('#dlg-plan .plan-warn li[data-code="E40_OFFSET"].bad').count(), 1)
                self.assertIn("clamped", page.locator('#dlg-plan .plan-warn li[data-code="CLAMPED"] .tn-badge').first.inner_text())
                self.assertIn("highest requested VID among units", page.inner_text("#dlg-plan .plan-vccia"))   # G-128 label; margin_v is null: no headroom text, no "null"
                self.assertNotIn("regulator headroom", page.inner_text("#dlg-plan .plan-vccia"))
                self.assertNotRegex(page.inner_text("#dlg-plan .plan-vccia"), r"null|NaN|undefined")
                self.assertIn("1\u20132 cores loaded", page.inner_text("#dlg-plan .plan-vccia"))
                page.keyboard.press("Escape")
                # G-117: a turbo-only edit sends {turbo} alone and the review describes plain MSR writes
                page.click("#btn-tn-discard")
                page.locator("#tn-turbo .turbo-kind").nth(0).locator("select").nth(1).select_option("53")
                page.click("#btn-tn-plan")
                page.wait_for_selector("#dlg-plan[open]")
                dlg = page.inner_text("#dlg-plan")
                self.assertIn("Turbo table changes", dlg)
                self.assertIn("Plain MSR table writes, no mailbox request", dlg)
                self.assertNotIn("Show unchanged points", dlg)
                page.keyboard.press("Escape")
                # F3 / F7 / F9 on Curves
                page.click("#tab-curves")
                page.wait_for_selector("#cv-chart .vc-nm", timeout=5000)
                page.locator("#cv-primary").select_option(label=page.locator("#cv-primary option:has-text('baseline')").first.inner_text())
                page.wait_for_selector("#cv-chart .vc-pin.fused", timeout=5000)
                self.assertEqual(page.locator("#cv-chart .vc-pin.fused").count(), 12)
                # G-131: nothing unmeasured is drawn inside the plot; a "not measured" row under the axis carries a tick per point, with a tooltip, and one caption line groups them
                self.assertEqual(page.locator("#cv-chart .vc-pin.reach").count(), 0)
                titles = page.locator("#cv-chart .vc-nm title").all_text_contents()
                self.assertIn("P1 56\u00d7: above this core's max ratio 55\u00d7", titles)
                self.assertIn("P1 57\u00d7: above this core's max ratio 55\u00d7", titles)
                self.assertFalse(any(t.startswith("EC0 23\u00d7") for t in titles), titles)   # G-132: E 23x is interpolated, so it has a value and is no longer "not measured"
                self.assertEqual(page.locator("#cv-chart .vc-nm-label").text_content(), "not measured")
                caption = page.inner_text("#cv-unreach")
                self.assertTrue(caption.startswith("not measured: "), caption)
                self.assertIn("at 56\u201357\u00d7 (above core max 55\u00d7)", caption)
                self.assertNotIn("Hollow rings", caption)
                self.assertNotIn("cpufreq_granularity", caption)   # the caption speaks plainly, the codes stay in the table view
                self.assertIn("fused V/F table as applied", page.inner_text("#cv-base-note"))
            finally:
                b.close()
        errors = [m for m in logs if m[0] in ("error", "pageerror", "warning") and "favicon" not in m[1] and "404" not in m[1]]
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
