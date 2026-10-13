"""G-123 page: the stored read on load (tune.last), profile contents / chain / compare / Load into Tune (profiles.get), the budget as an indicator (D-032),
the real read estimate and the default curve. Static checks always; the browser flow runs with ARLTUNER_BROWSER_TESTS=1 (playwright + Chrome) against the
contract-9 fake helper. No hardware, no real helper."""
from __future__ import annotations

import os
import re
import shutil
import time
import unittest

from tests.web.test_dashboard_static import CSS, HTML, JS, func_body

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

CHROME = os.environ.get("ARLTUNER_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or ""


class TestStatic(unittest.TestCase):
    def test_the_stored_read_is_asked_for_once_and_never_over_an_editor(self):
        self.assertEqual(len(re.findall(r'\bapi\(\s*"tune\.last"', JS)), 3)   # the load, (G-129) the compare redone after Set as accepted, and the turbo tables after an apply
        self.assertNotIn("setWords(", func_body("refreshTablesAfterApply"))   # it only swaps the tables, never the editor
        refresh = func_body("refreshAcceptedCompare")
        self.assertNotIn("setWords(", refresh)                 # it only swaps the accepted comparison of the words already shown
        self.assertNotIn("buildRows(", refresh)
        body = func_body("loadLastRead")
        self.assertIn("T.words || S.lastTried", body)        # a reconnect must not wipe the editor
        self.assertIn("if (T.words ||", body)                # a read that finished meanwhile wins
        self.assertIn("available !== true", body)
        self.assertIn("loadLastRead();", func_body("onOpen"))
        self.assertIn("renderProvenance();", func_body("setWords"))   # "Read from the CPU at HH:MM" / "Showing the helper's earlier read from HH:MM"
        self.assertIn("Re-read ratios & voltages", func_body("setWords"))

    def test_loading_a_profile_into_tune_sends_nothing(self):
        body = func_body("loadIntoTune")
        self.assertNotIn("api(", body)                       # it only fills the editor; Review changes -> Apply is the only way to write
        self.assertNotIn("requestPlan", body)
        self.assertIn("setPoint(", body)
        self.assertIn("confirmDialog(", body)                # replaces pending edits only after a yes
        self.assertEqual(len(re.findall(r'\bapi\(\s*"profiles\.get"', JS)), 1)
        self.assertIn("info ? info.contents : null", body)   # G-148: a profile is its own state (no chain to accumulate); a complete one names every unit
        self.assertNotIn("cumulative", JS)

    def test_the_budget_is_an_indicator_not_a_countdown(self):
        for gone in ('id="ring-ov"', 'id="ring-sys"', 'id="ov-left"', 'id="hb-left"', 'id="hb-budget"'):
            self.assertNotIn(gone, HTML)
        self.assertNotIn("setRing(", JS)
        self.assertIn("Mailbox ops this boot", HTML)
        self.assertIn('id="sp-ops-ov"', HTML)
        self.assertIn('id="sp-ops-sys"', HTML)
        body = func_body("renderHelper")
        self.assertIn('setText("ov-used"', body)
        self.assertIn('setText("hb-cap"', body)              # the per-boot cap is a note
        self.assertIn("opsLeft() < readCost()", func_body("gateReason"))   # the guard's limit still gates a read that cannot fit (the cost comes from the last read)
        self.assertIn(".ops-line", CSS)

    def test_the_read_cost_is_computed_not_a_fixed_estimate(self):
        """D-036: no fixed 124-136 operations; the count is the last read's own (ops_done), or one per V/F point, or 'about one per point' before any read."""
        self.assertNotIn("124", HTML)
        self.assertNotIn("COST_LOW", JS)
        self.assertNotIn("COST_HIGH", JS)
        self.assertIn('id="tn-cost-n"', HTML)
        self.assertIn("ops_done", func_body("readCost"))
        self.assertIn("about one operation per V/F point", HTML)

    def test_default_curve_is_the_newest_baseline_else_the_newest_complete_all_units_curve(self):
        body = func_body("defaultCurveId")
        self.assertLess(body.index("baselineId()"), body.index("UNIT_IDS.every"))
        self.assertIn("c.complete !== false", body)
        self.assertGreaterEqual(JS.count("defaultCurveId()"), 4)   # Curves, the Tune preview (twice) and the definition's callers
        self.assertNotIn("CV.list.length ? CV.list[0].id : null;\n    sel.value", JS)

    def test_profiles_page_has_the_accepted_profile_and_compare_table(self):
        for ident in ("pr-accepted", "pr-accepted-card", "pr-diff", "pr-load-status"):
            self.assertIn(f'id="{ident}"', HTML)
        for gone in ('id="pr-chain"', 'id="pr-basis"', "As applied", "This file only"):   # G-148: no chain UI, no compare basis toggle
            self.assertNotIn(gone, HTML + JS)
        self.assertNotIn(".pr-step", CSS)
        self.assertIn("Change", HTML)
        self.assertIn("compareRows(", func_body("renderProfileDiff"))
        self.assertIn('"partial (legacy)"', func_body("kindPill"))


@unittest.skipUnless(os.environ.get("ARLTUNER_BROWSER_TESTS") == "1" and sync_playwright and CHROME, "set ARLTUNER_BROWSER_TESTS=1 (needs playwright + chrome)")
class TestBrowser(unittest.TestCase):
    @staticmethod
    def until(cond, timeout=8.0):  # page.wait_for_function evaluates a string, which the page's CSP forbids
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.1)
        raise AssertionError("condition not reached")

    def test_stored_read_accepted_compare_load_and_budget(self):
        from tests.web import v3_harness as h

        srv, fake, stop = h.start(apply_delay=0.01, seed=False)
        fake.seed_accepted_reference()
        fake.profiles["old-p5"] = {"name": "old-p5", "source": "saved", "created": "2026-10-03T10:00:00Z",   # a profile saved before G-148: partial (legacy)
                                   "intent": {"scope": "unit", "rows": [{"target": "P5", "offset": {"kind": "points", "mv": {"55": -50}}, "v_target_v": None, "ratio": None}]}}
        fake.seed_last_read(time.time() - 600)
        fake.ops_used_v3 = 262
        self.addCleanup(fake.close)
        self.addCleanup(srv.close)
        self.addCleanup(stop.set)
        logs = []
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
            try:
                page = b.new_context(viewport={"width": 1400, "height": 900}).new_page()
                page.on("console", lambda m: logs.append((m.type, m.text)))
                page.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
                h.login(page, srv)
                page.goto(srv.origin + "/")
                page.wait_for_selector("#conn-helper.s-ok", timeout=10000)
                # the stored read pre-populates Tune without a mailbox read
                page.click("#tab-tune")
                page.wait_for_selector("#tn-console:not([hidden])", timeout=8000)
                self.assertIn("earlier read from", page.inner_text("#tn-prov"))   # the stored read is labelled as the helper's earlier read
                self.assertIn("no mailbox operations used", page.inner_text("#tn-prov"))
                self.assertIn("Re-read", page.inner_text("#btn-tune-read"))
                self.assertNotIn("unknown", page.inner_text("#tn-state"))
                self.assertEqual([r["op"] for r in fake.requests if r["op"] == "tune.read"], [])
                self.assertEqual(len([r for r in fake.requests if r["op"] == "tune.last"]), 1)
                # the budget is a count
                page.click("#tab-dashboard")
                self.until(lambda: page.inner_text("#ov-used") == "262")
                self.assertEqual(page.inner_text("#ov-used"), "262")
                self.assertGreater(page.locator("#sp-ops-ov path").count(), 0)
                # profiles: the one accepted profile, per-profile summary and kind, compare, load
                page.click("#tab-profiles")
                page.wait_for_selector("#pr-accepted .pr-accepted-name", timeout=8000)
                self.assertEqual(page.locator("#pr-accepted .pr-accepted-name").all_inner_texts(), ["accepted-2026-10-04"])
                self.until(lambda: "reading" not in page.inner_text("#pr-list"))
                self.assertIn("deepest", page.inner_text("#pr-list"))
                self.assertIn("partial (legacy)", page.locator("#pr-list li", has_text="old-p5").inner_text())
                page.select_option("#pr-a", "accepted-2026-10-04")
                page.select_option("#pr-b", "old-p5")
                page.wait_for_selector("#pr-diff:not([hidden])")
                self.assertIn("Offset at 55", page.inner_text("#pr-diff-body"))
                self.assertIn("+23", page.inner_text("#pr-diff-body"))   # P5 55x: -73 accepted, -50 in the old profile
                self.assertIn("partial (legacy)", page.inner_text("#pr-diff-note"))
                page.locator("#pr-list li", has_text="accepted-2026-10-04").locator("button:has-text('Load into Tune')").click()
                page.wait_for_selector("#panel-tune:not([hidden])")
                self.assertIn("Loaded \"accepted-2026-10-04\"", page.inner_text("#tn-status"))
                self.assertIn("units changed", page.inner_text("#tn-foot-msg"))
                self.assertEqual([r["op"] for r in fake.requests if r["op"] in ("tune.apply", "tune.plan")], [])   # nothing sent by loading
            finally:
                b.close()
        errors = [m for m in logs if m[0] in ("error", "pageerror", "warning") and "favicon" not in m[1] and "404" not in m[1]]
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
