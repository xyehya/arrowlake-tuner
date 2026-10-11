"""G-113 browser smoke (opt-in): drives the real page in headless Chrome against the contract-9 fake helper (``tests/web/v3_harness.py``).

Skipped unless ``ARLTUNER_BROWSER_TESTS=1`` and Playwright (python) plus a Chrome/Chromium binary (``ARLTUNER_CHROME`` or ``/usr/bin/google-chrome``) exist.
Asserts what static checks cannot: no console errors / CSP violations, every tab renders, the Curves chart fills while measuring, Tune can read, plan,
confirm and apply in the fake, and the Apply button is one click (no typed code). No hardware, no real helper."""
from __future__ import annotations

import os
import shutil
import threading
import time
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

CHROME = os.environ.get("ARLTUNER_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or ""


@unittest.skipUnless(os.environ.get("ARLTUNER_BROWSER_TESTS") == "1" and sync_playwright and CHROME, "set ARLTUNER_BROWSER_TESTS=1 (needs playwright + chrome)")
class TestBrowser(unittest.TestCase):
    def test_pages_measure_tune_apply_flow(self):
        from tests.web import v3_harness as h

        srv, fake, stop = h.start(measure_delay=0.01, apply_delay=0.05, seed=False)
        fake.record_budget_units = 4  # G-111: records come in pieces (omitted_units); the page must merge them
        self.addCleanup(fake.close)
        self.addCleanup(srv.close)
        self.addCleanup(stop.set)
        logs: list[tuple[str, str]] = []
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
            try:
                page = b.new_context(viewport={"width": 1400, "height": 900}).new_page()
                page.on("console", lambda m: logs.append((m.type, m.text)))
                page.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
                h.login(page, srv)
                page.goto(srv.origin + "/")
                page.wait_for_selector("#conn-helper.s-ok", timeout=10000)
                for tab in ("curves", "tune", "profiles", "system", "audit", "dashboard"):
                    page.click("#tab-" + tab)
                    self.assertTrue(page.locator("#panel-" + tab).is_visible(), tab)
                # memory card from mem.read
                page.wait_for_selector("#mem-body .mem-big", timeout=5000)
                self.assertIn("DDR5-7200", page.inner_text("#mem-body"))
                # measure: the chart fills and a curve is stored
                page.click("#tab-curves")
                page.wait_for_selector("#btn-measure:not([disabled])", timeout=10000)
                page.click("#btn-measure")
                page.wait_for_selector("#cv-chart .vc-line", timeout=10000)
                page.wait_for_selector("#btn-measure:not([disabled])", timeout=30000)
                self.assertEqual(len(fake.curves), 1)
                self.assertEqual(page.locator("#cv-chart .vc-line").count(), 12)  # all 12 units, merged from the pieces
                # tune: read, edit, review, confirm, apply
                page.click("#tab-tune")
                page.wait_for_selector("#btn-tune-read:not([disabled])", timeout=10000)
                page.click("#btn-tune-read")
                page.wait_for_selector("#tn-console:not([hidden])", timeout=15000)
                page.locator(".tn-rowwrap").nth(0).locator("select.tn-sel").first.select_option("-45")
                page.click("#btn-tn-plan")
                page.wait_for_selector("#dlg-plan[open]")
                self.assertEqual(page.locator("#dlg-plan input").count(), 0)  # D-033 d: nothing to type
                page.wait_for_function("!document.getElementById('plan-go').disabled")
                page.click("#plan-go")
                page.wait_for_selector(".plan-result .pill.s-ok", timeout=10000)
                self.assertEqual(fake.tuning["P0"]["points"][0]["offset_mv"], -45.0)
                self.assertEqual([r["op"] for r in fake.requests if r["op"] == "tune.apply"], ["tune.apply"])
                # validate: the review window offers a stress screen of the changed units; nothing starts by itself
                page.click("text=Validate with a stress screen")
                page.wait_for_selector("#panel-stress:not([hidden])")
                self.assertEqual(page.locator('.mm-tile[aria-pressed="true"]').count(), 8)  # the first row of the simple scope is "all P-cores"
                self.assertEqual([r["op"] for r in fake.requests if r["op"] == "stress.start"], [])
                self.assertIn("NOT an acceptance gate", page.inner_text("#st-banner"))
                page.wait_for_selector("#btn-stress-start:not([disabled])", timeout=10000)
                page.click("#btn-stress-start")
                page.wait_for_selector("#st-result:not([hidden])", timeout=15000)
                self.assertIn("No errors detected in", page.inner_text("#st-result"))
                self.assertNotIn("pass", page.inner_text("#panel-stress").lower())
                start = [r for r in fake.requests if r["op"] == "stress.start"]
                self.assertEqual((len(start), start[0]["args"]["units"], start[0]["args"]["duration_s"]), (1, ["P%d" % i for i in range(8)], 300))
                page.wait_for_selector("#st-hist-body tr", timeout=5000)
                time.sleep(0.3)
            finally:
                b.close()
        errors = [m for m in logs if m[0] in ("error", "pageerror", "warning") and "favicon" not in m[1] and "404" not in m[1]]
        self.assertEqual(errors, [])

    def test_apply_shows_the_os_frequency_cap_table(self):
        """G-126: an OC ratio change makes the helper report ``cpufreq``; the read-back dialog shows it (this builds ``cpufreqNode`` for real)."""
        from tests.web import v3_harness as h

        srv, fake, stop = h.start(measure_delay=0.01, apply_delay=0.01, seed=False)
        self.addCleanup(fake.close)
        self.addCleanup(srv.close)
        self.addCleanup(stop.set)
        logs: list[tuple[str, str]] = []
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
            try:
                page = b.new_context(viewport={"width": 1400, "height": 900}).new_page()
                page.on("console", lambda m: logs.append((m.type, m.text)))
                page.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
                h.login(page, srv)
                page.goto(srv.origin + "/")
                page.wait_for_selector("#conn-helper.s-ok", timeout=10000)
                page.click("#tab-tune")
                page.wait_for_selector("#btn-tune-read:not([disabled])", timeout=10000)
                page.click("#btn-tune-read")
                page.wait_for_selector("#tn-console:not([hidden])", timeout=15000)
                row = page.locator(".tn-rowwrap").nth(1)  # all E-clusters: OC ratio not set -> 49x (cpuinfo 4.9 -> 5.0 GHz) at 1.165 V
                row.locator("select.tn-sel").nth(1).select_option("1.165")
                row.locator("select.tn-sel").nth(2).select_option("49")
                page.click("#btn-tn-plan")
                page.wait_for_selector("#dlg-plan[open]")
                page.wait_for_function("!document.getElementById('plan-go').disabled")
                page.click("#plan-go")
                page.wait_for_selector(".plan-result .cpufreq-box .pill.s-ok", timeout=10000)
                box = page.inner_text(".cpufreq-box")
                self.assertIn("4.90 GHz", box)
                self.assertIn("5.00 GHz", box)
                self.assertIn("raised", box)
                self.assertEqual(page.locator(".cpufreq-box tbody tr").count(), 16)  # the 16 E-cores
            finally:
                b.close()
        errors = [m for m in logs if m[0] in ("error", "pageerror", "warning") and "favicon" not in m[1] and "404" not in m[1]]
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
