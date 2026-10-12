"""G-139 Tune polish after the live check of G-133: the kept read shows on the first visit (tune.last is asked again when the helper refuses it BUSY, and when the Tune tab opens with
nothing shown), point cells keep one width, an inactive retained adaptive voltage reads "not set", and the read-cost text is computed from the live unit list.

Browser checks need ``ARLTUNER_BROWSER_TESTS=1`` (Playwright + Chrome); fake helper only, no hardware."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

HERE = Path(__file__).resolve().parent
JS = (HERE.parents[1] / "web" / "app.js").read_text(encoding="utf-8")
FIXTURE = json.loads((HERE / "fixtures" / "tune-last-285k-2026-10-12.json").read_text(encoding="utf-8"))["result"]
CHROME = os.environ.get("ARLTUNER_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or ""
ROW = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "EC0": 8, "EC1": 9, "EC2": 10, "EC3": 11}


class TestStatic(unittest.TestCase):
    def test_the_kept_read_is_asked_again_not_once(self):
        self.assertIn("LAST_RETRY_MS", JS)
        self.assertIn("S.lastBusy", JS)
        self.assertIn("function readExtraOps", JS)


def browser(test):
    return unittest.skipUnless(os.environ.get("ARLTUNER_BROWSER_TESTS") == "1" and sync_playwright and CHROME, "set ARLTUNER_BROWSER_TESTS=1 (needs playwright + chrome)")(test)


class TestBrowser(unittest.TestCase):
    @contextlib.contextmanager
    def session(self, *, fixture=True, last=True, busy_first=0, layout=None, tab=True):
        from tests.web import v3_harness as h

        srv, fake, stop = h.start(seed=False, layout=layout)
        fake.last_busy_first = busy_first
        if fixture:
            fake.fixture_result = json.loads(json.dumps(FIXTURE))
        if last:
            fake.seed_last_read(time.time() - 120)
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
                if tab:
                    page.click("#tab-tune")
                yield page, fake
            finally:
                b.close()
        errors = [m for m in logs if m[0] in ("error", "pageerror", "warning") and "favicon" not in m[1] and "404" not in m[1] and "409" not in m[1]]
        self.assertEqual(errors, [])

    # ---- 1: the kept read on the first visit ----------------------------------------------------------------------------------------------------------------
    @browser
    def test_a_busy_helper_does_not_leave_the_first_visit_empty(self):
        with self.session(busy_first=3) as (page, fake):
            page.wait_for_selector("#tn-console:not([hidden])", timeout=15000)   # three refusals, then the kept read: no reload
            self.assertIn("Showing the read kept by the helper", page.inner_text("#tn-status"))
            self.assertNotIn("Trying again", page.inner_text("#tn-status"))
            self.assertEqual(len([r for r in fake.requests if r["op"] == "tune.last"]), 4)
            self.assertEqual([r for r in fake.requests if r["op"] == "tune.read"], [])   # no mailbox operation

    @browser
    def test_while_the_helper_refuses_the_page_says_so_instead_of_staying_blank(self):
        with self.session(busy_first=2) as (page, fake):
            page.wait_for_function("document.getElementById('tn-status').textContent.indexOf('Trying again') >= 0 || !document.getElementById('tn-console').hidden", timeout=8000)
            page.wait_for_selector("#tn-console:not([hidden])", timeout=15000)

    @browser
    def test_opening_the_tune_tab_asks_again_when_nothing_is_shown(self):
        with self.session(last=False, tab=False) as (page, fake):
            page.click("#tab-tune")
            time.sleep(0.5)
            self.assertTrue(page.locator("#tn-console").is_hidden())     # no read kept yet
            fake.seed_last_read(time.time() - 60)                          # another page read meanwhile
            page.click("#tab-dashboard")
            time.sleep(2.3)
            page.click("#tab-tune")
            page.wait_for_selector("#tn-console:not([hidden])", timeout=8000)
            self.assertIn("Showing the read kept by the helper", page.inner_text("#tn-status"))

    # ---- 2: one width for every point cell ------------------------------------------------------------------------------------------------------------------
    @browser
    def test_point_cells_have_one_width_and_a_short_tag(self):
        with self.session() as (page, fake):
            page.wait_for_selector("#tn-console:not([hidden])", timeout=10000)
            page.click("#tn-scope button:has-text('Per-Core')")
            for unit in ("P1", "P2", "EC1"):
                page.locator(".tn-rowwrap").nth(ROW[unit]).locator(".seg button:has-text('Per V/F point')").click()
                cells = page.locator("#tn-pts-" + unit + " > .tn-pt:not(.tn-oc)")
                widths = [round(cells.nth(i).bounding_box()["width"], 1) for i in range(cells.count())]
                self.assertGreaterEqual(len(widths), 6, unit)
                self.assertEqual(len(set(widths)), 1, (unit, widths))
            over = page.locator("#tn-pts-P1 .tn-pt.unreach")
            self.assertEqual(over.count(), 2)
            tag = over.first.locator(".tn-pt-tag")
            self.assertEqual(tag.inner_text(), "above 55× limit")
            self.assertIn("own 0x1C ratio (55×)", tag.get_attribute("title"))
            self.assertIn("fused · offset only", tag.get_attribute("title"))

    # ---- 3: a retained target is not "now" ------------------------------------------------------------------------------------------------------------------
    @browser
    def test_a_retained_target_without_an_oc_ratio_reads_not_set_until_the_raw_view(self):
        with self.session() as (page, fake):
            page.wait_for_selector("#tn-console:not([hidden])", timeout=10000)
            page.click("#tn-scope button:has-text('Per-Core')")
            sel = lambda unit: page.locator(".tn-rowwrap").nth(ROW[unit]).locator("select.tn-sel").nth(2)   # noqa: E731
            chosen = "s => s.selectedOptions[0].textContent.trim()"
            self.assertTrue(sel("EC2").is_disabled())
            self.assertEqual(sel("EC2").evaluate(chosen), "not set")            # EC2: ratio = the fused top, 1.1367 V kept in the word
            self.assertNotIn("now", sel("EC2").evaluate(chosen))
            self.assertIn("1.1367 V", sel("EC1").evaluate(chosen))               # EC1 is a real OC point: unchanged
            self.assertIn("now", sel("EC1").evaluate(chosen))
            page.click("#btn-tn-hex")
            self.assertEqual(sel("EC2").evaluate(chosen), "1.1367 V (inactive)")
            self.assertIn("now", sel("EC1").evaluate(chosen))
            page.click("#btn-tn-hex")
            self.assertEqual(sel("EC2").evaluate(chosen), "not set")
            page.locator(".tn-rowwrap").nth(ROW["EC2"]).locator("select.tn-sel").nth(1).select_option("48")   # an OC ratio makes the kept target the value again
            self.assertFalse(sel("EC2").is_disabled())
            self.assertEqual(sel("EC2").input_value(), "1.1367")
            self.assertIn("1.1367 V", sel("EC2").evaluate(chosen))

    # ---- 4: the read cost from the live unit list -----------------------------------------------------------------------------------------------------------
    @browser
    def test_the_read_cost_before_a_read_comes_from_the_unit_list(self):
        with self.session(last=False) as (page, fake):
            text = page.inner_text("#tn-cost")
            self.assertIn("about one operation per V/F point of each of the 12 units (8 P-cores, 4 E-clusters), plus 32 for the fused points, favored ratios, limits and turbo tables", text)
            self.assertNotIn("132", text)
        with self.session(last=False, layout=(6, [[6, 7, 8, 9], [10, 11, 12, 13]])) as (page, fake):
            page.wait_for_function("document.getElementById('tn-cost-n').textContent.indexOf('8 units') >= 0", timeout=8000)
            self.assertIn("each of the 8 units (6 P-cores, 2 E-clusters), plus 24 for", page.inner_text("#tn-cost"))   # 6+2 fused, 6+2 favored, 2 kinds x (0x01, 0x28), 4 turbo MSRs


if __name__ == "__main__":
    unittest.main()
