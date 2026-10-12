"""G-132 on the page: interpolated V/F points (hollow markers, chart of V/F points only), the turbo-off note on Curves, "(interpolated)" on Tune, and the reason texts.

Static checks always run; the browser checks need ``ARLTUNER_BROWSER_TESTS=1`` (Playwright + Chrome) and use the fake helper. No hardware, no real helper code."""
from __future__ import annotations

import contextlib
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


def body_of(name: str) -> str:
    m = re.search(r"function " + re.escape(name) + r"\(.*?\) \{(.*?)\n  \}\n", JS, re.S)
    assert m, name
    return m.group(1)


class TestStatic(unittest.TestCase):
    def test_reason_texts_cover_the_new_reasons(self):
        body = body_of("reasonText")
        for reason in ("cap_granularity", "cpufreq_granularity", "turbo_off", "user_cap", "max_turbo", "active_cores"):
            self.assertIn(reason, body, reason)
        self.assertIn("Linux cannot cap this core to exactly", body)
        self.assertIn("the nearest are", body)
        self.assertIn("exact: both neighbours equal", body_of("interpText"))

    def test_the_chart_plots_v_f_points_only_and_interpolated_ones_are_hollow(self):
        self.assertIn("function recChartPoints", JS)
        self.assertIn("recChartPoints(a, u)", body_of("curvesModel"))
        self.assertIn(".vc-dot.interp", CSS)
        self.assertIn('id="cv-turbo-note"', HTML)
        self.assertIn('p.status === "interpolated"', body_of("baseVid") if "function baseVid" in JS else JS)


def browser(test):
    return unittest.skipUnless(os.environ.get("ARLTUNER_BROWSER_TESTS") == "1" and sync_playwright and CHROME, "set ARLTUNER_BROWSER_TESTS=1 (needs playwright + chrome)")(test)


class TestBrowser(unittest.TestCase):
    def start(self, **fake_attrs):
        from tests.web import v3_harness as h

        srv, fake, stop = h.start(seed=True)
        for k, v in fake_attrs.items():
            setattr(fake, k, v)
        self.addCleanup(fake.close)
        self.addCleanup(srv.close)
        self.addCleanup(stop.set)
        return h, srv, fake

    @contextlib.contextmanager
    def session(self, h, srv, logs):
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
            try:
                page = b.new_context(viewport={"width": 1400, "height": 1000}).new_page()
                page.on("console", lambda m: logs.append((m.type, m.text)))
                page.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
                h.login(page, srv)
                page.goto(srv.origin + "/")
                page.wait_for_selector("#conn-helper.s-ok", timeout=10000)
                yield page
            finally:
                b.close()

    def assert_no_errors(self, logs):
        errors = [m for m in logs if m[0] in ("error", "pageerror", "warning") and "favicon" not in m[1] and "404" not in m[1]]
        self.assertEqual(errors, [])

    @browser
    def test_the_chart_draws_v_f_points_only_with_hollow_interpolated_markers_and_the_table_keeps_everything(self):
        h, srv, fake = self.start()
        rec = fake.seed_curve("Dense baseline", mode="dense", baseline=True, offset_mv=0.0, units=["P1", "EC0"])   # (a dense record of all 12 units exceeds one 64 KiB line)
        e = {p["ratio"]: p for p in rec["units"]["EC0"]["points"]}
        self.assertEqual(e[23]["status"], "interpolated")
        logs = []
        with self.session(h, srv, logs) as page:
            page.click("#tab-curves")
            page.wait_for_selector("#cv-primary option:has-text('Dense baseline')", state="attached", timeout=10000)
            page.locator("#cv-primary").select_option(label=page.locator("#cv-primary option:has-text('Dense baseline')").first.inner_text())
            page.wait_for_selector("#cv-chart .vc-dot", timeout=5000)
            # (b) only the V/F points: 8 on each P-core, 6 on each E-cluster; none of the in-between ratios of the dense run
            self.assertEqual(page.locator("#cv-chart .vc-dot.u-P1").count(), 8 - 2)   # a non-favored P-core cannot hold 56x / 57x: those two are not measured, so not drawn
            self.assertEqual(page.locator("#cv-chart .vc-dot.u-EC0").count(), 6)
            self.assertEqual(page.locator("#cv-chart .vc-dot.between").count(), 0)
            self.assertEqual(page.locator("#cv-chart .vc-dot.u-EC0.interp").count(), 1)   # E 23x: hollow marker on the line
            titles = page.locator("#cv-chart .vc-dot.u-EC0.interp title").all_text_contents()
            self.assertEqual(len(titles), 1)
            self.assertIn("interpolated between 15× and 30×", titles[0])
            self.assertIn("interpolated (hollow markers): EC0 at 23\u00d7", page.inner_text("#cv-unreach"))
            # the "not measured" row keeps only V/F points that have no value at all (no E 23x, no P 52x)
            nm = page.locator("#cv-chart .vc-nm title").all_text_contents()
            self.assertTrue(nm and all("56×" in t or "57×" in t for t in nm), nm)
            self.assertFalse(any("52" in t for t in nm), nm)
            # the Table view still carries every ratio, the interpolated one marked with ~ and explained
            page.click("#cv-view button:has-text('Table')")
            page.wait_for_selector("#cv-table th:has-text('10×')", timeout=5000)
            cell = page.locator("#cv-table tbody tr", has=page.locator("th", has_text="EC0")).locator("td").nth(23 - 8)
            self.assertTrue(cell.inner_text().endswith("~"), cell.inner_text())
            self.assertIn("interpolated between 15× and 30×", cell.get_attribute("title"))
            p52 = page.locator("#cv-table tbody tr", has=page.locator("th", has_text="P1")).locator("td").nth(52 - 8)
            self.assertIn("cap_granularity", p52.get_attribute("title"))
            self.assertIn("Linux cannot cap this core to exactly 52×", p52.get_attribute("title"))
            self.assertIn("the nearest are 51× and 53×", p52.get_attribute("title"))
        self.assert_no_errors(logs)

    @browser
    def test_the_turbo_off_note_shows_on_curves_before_a_run(self):
        h, srv, fake = self.start(no_turbo=True)
        logs = []
        with self.session(h, srv, logs) as page:
            page.click("#tab-curves")
            page.wait_for_selector("#cv-turbo-note:not([hidden])", timeout=10000)
            self.assertEqual(page.inner_text("#cv-turbo-note"), "Turbo is off (power-saver profile?): ratios above base cannot be reached — switch to Balanced or Performance")
        self.assert_no_errors(logs)

    @browser
    def test_no_note_when_turbo_is_on(self):
        h, srv, fake = self.start()
        logs = []
        with self.session(h, srv, logs) as page:
            page.click("#tab-curves")
            page.wait_for_selector("#cv-chart", timeout=10000)
            page.wait_for_timeout(300)
            self.assertTrue(page.locator("#cv-turbo-note").is_hidden())
        self.assert_no_errors(logs)

    @browser
    def test_tune_predictions_use_the_interpolated_value_and_say_so(self):
        h, srv, fake = self.start()
        fake.seed_curve("Stock baseline", baseline=True, offset_mv=0.0)   # quick: E 23x is interpolated between 15x and 30x
        logs = []
        with self.session(h, srv, logs) as page:
            page.click("#tab-tune")
            page.wait_for_selector("#btn-tune-read:not([disabled])", timeout=10000)
            page.click("#btn-tune-read")
            page.wait_for_selector("#tn-console:not([hidden])", timeout=15000)
            page.click("#tn-scope button:has-text('Per unit')")
            page.locator(".tn-rowwrap").nth(9).locator(".seg button:has-text('Per point')").click()
            eff = page.locator("#tn-pts-EC1 .tn-pt").nth(2).locator(".tn-pt-eff")   # point 3 = 23x
            page.wait_for_function("(el) => el.textContent.includes('interpolated')", arg=eff.element_handle(), timeout=10000)
            self.assertRegex(eff.inner_text(), r"^→ \d\.\d{3} V \(interpolated\)$")
            self.assertIn("interpolated between the neighbouring measured points", eff.get_attribute("title"))
            other = page.locator("#tn-pts-EC1 .tn-pt").nth(3).locator(".tn-pt-eff").inner_text()   # 30x is measured
            self.assertNotIn("interpolated", other)
        self.assert_no_errors(logs)


if __name__ == "__main__":
    unittest.main()
