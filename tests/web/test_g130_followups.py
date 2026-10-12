"""G-130: web follow-ups to G-127 / G-128, against the contract-9 fake helper (opt-in browser tests: ``ARLTUNER_BROWSER_TESTS=1``, Playwright + Chrome).

1. The apply race: with a fast helper the tune.apply result (and progress) can reach the page over SSE before the POST's 202 does; the review dialog must still finish.
2. Preflight status ``"warn"`` renders as an amber pill (not unknown / fail) and does not change the mode.
3. G-128 VccIA: ``margin_v`` and ``contributors.ring`` are null; the page shows neither "null" nor NaN.
4. The unit list comes from ``preflight.topology.units`` (6P + 2 E-clusters here): every tab shows 8 units and the console stays clean.
No hardware, no real helper code."""
from __future__ import annotations

import contextlib
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

WEB = Path(__file__).resolve().parents[2] / "web"
JS = (WEB / "app.js").read_text(encoding="utf-8")
CSS = (WEB / "style.css").read_text(encoding="utf-8")
CHROME = os.environ.get("ARLTUNER_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or ""
LAYOUT_6P_2E = (6, [[6, 7, 8, 9], [10, 11, 12, 13]])   # EC0 = cpus 6-9, EC1 = cpus 10-13


class TestStatic(unittest.TestCase):
    def test_warn_is_styled_like_the_other_amber_pills(self):
        self.assertRegex(CSS, r"\.pill\.s-warn\b")
        self.assertIn("warn: 3", JS)   # sorted between unknown and missing

    def test_the_unit_list_is_one_mutable_array_fed_by_the_topology(self):
        self.assertIn("function unitsFromTopology", JS)
        self.assertIn("applyUnits(isObj(r) ? r.topology : null)", JS)
        self.assertNotIn("UNIT_IDS.slice(0, 8)", JS)
        self.assertNotIn("UNIT_IDS.slice(8)", JS)

    def test_events_that_beat_the_202_are_kept(self):
        self.assertIn("S.early", JS)
        self.assertIn("S.submitKind", JS)


@unittest.skipUnless(os.environ.get("ARLTUNER_BROWSER_TESTS") == "1" and sync_playwright and CHROME, "set ARLTUNER_BROWSER_TESTS=1 (needs playwright + chrome)")
class TestBrowser(unittest.TestCase):
    @staticmethod
    def until(cond, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.1)
        raise AssertionError("condition not reached")

    @contextlib.contextmanager
    def session(self, *, apply_delay=0.3, layout=None, seed=True, prepare=None):
        from tests.web import v3_harness as h

        srv, fake, stop = h.start(seed=seed, apply_delay=apply_delay, layout=layout)
        if prepare is not None:   # the web server keeps the first preflight answer: change the fake before the page loads
            prepare(fake)
        self.addCleanup(fake.close)
        self.addCleanup(srv.close)
        self.addCleanup(stop.set)
        logs: list[tuple[str, str]] = []
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
            try:
                page = b.new_context(viewport={"width": 1400, "height": 1000}).new_page()
                page.set_default_timeout(10000)
                page.on("console", lambda m: logs.append((m.type, m.text)))
                page.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
                h.login(page, srv)
                page.goto(srv.origin + "/")
                page.wait_for_selector("#conn-helper.s-ok", timeout=10000)
                yield page, fake
            finally:
                b.close()
        errors = [m for m in logs if m[0] in ("error", "pageerror", "warning") and "favicon" not in m[1] and "404" not in m[1]]
        self.assertEqual(errors, [])

    def read(self, page):
        page.click("#tab-tune")
        page.wait_for_selector("#btn-tune-read:not([disabled])", timeout=10000)
        page.click("#btn-tune-read")
        page.wait_for_selector("#tn-console:not([hidden])", timeout=15000)

    def plan_and_apply(self, page):
        page.locator(".tn-rowwrap").nth(0).locator("select.tn-sel").first.select_option("-45")
        page.click("#btn-tn-plan")
        page.wait_for_selector("#dlg-plan[open]")
        self.until(lambda: not page.locator("#plan-go").is_disabled())
        page.click("#plan-go")

    # ---- 1. the apply race ----
    def test_apply_finishes_with_a_fast_helper(self):
        for delay in (0.0, 0.01):
            for attempt in range(3):   # the race is timing dependent: several tries per delay
                with self.subTest(apply_delay=delay, attempt=attempt), self.session(apply_delay=delay, seed=False) as (page, fake):
                    self.read(page)
                    self.plan_and_apply(page)
                    page.wait_for_selector(".plan-result .pill.s-ok", timeout=8000)
                    self.assertEqual(fake.tuning["P0"]["points"][0]["offset_mv"], -45.0)
                    self.until(lambda: not page.locator("#btn-plan-close").is_disabled())
                    page.click("#btn-plan-close")
                    # the page is idle again: the next read is allowed
                    self.until(lambda: not page.locator("#btn-tune-read").is_disabled())

    # ---- 2. preflight "warn" ----
    def test_a_warn_check_is_amber_and_the_mode_stays(self):
        def prepare(fake):
            rep = fake.preflight_result
            fake.preflight_result = {**rep, "checks": rep["checks"] + [{"name": "cpu-stepping", "status": "warn", "reason": "stepping 3 is not on the validated list; mode unchanged"}]}

        with self.session(prepare=prepare) as (page, fake):
            page.click("#tab-system")
            pill = page.locator("#pf-body .pill.s-warn")
            self.assertEqual(pill.count(), 1)
            self.assertEqual(pill.inner_text(), "warn")
            self.assertEqual(page.locator("#pf-body .pill.s-unknown").count(), 0)
            warn_bg = pill.evaluate("e => getComputedStyle(e).backgroundColor")
            amber_bg = page.locator("#pf-body .pill.s-missing").first.evaluate("e => getComputedStyle(e).backgroundColor")
            self.assertEqual(warn_bg, amber_bg)
            self.assertNotEqual(warn_bg, page.locator("#pf-body .pill.s-pass").first.evaluate("e => getComputedStyle(e).backgroundColor"))
            self.assertIn("write", page.inner_text("#pf-summary"))
            # sorted ahead of "missing" and "pass" checks
            names = page.locator("#pf-body .check-name").all_inner_texts()
            self.assertLess(names.index("cpu-stepping"), names.index("stress-ng"))
            page.click("#tab-tune")
            self.assertEqual(page.locator("#b-readonly:visible, #b-refuse:visible").count(), 0)

    # ---- 3. VccIA without ring and margin ----
    def test_vccia_with_null_ring_and_margin_shows_no_null(self):
        with self.session() as (page, fake):
            self.read(page)
            page.locator(".tn-rowwrap").nth(0).locator("select.tn-sel").first.select_option("-45")
            page.click("#btn-tn-plan")
            page.wait_for_selector("#dlg-plan .plan-vccia")
            box = page.inner_text("#dlg-plan .plan-vccia")
            self.assertIn("Predicted VccIA", box)
            self.assertIn("highest requested VID among units", box)
            self.assertNotRegex(box, r"null|NaN|undefined|headroom")
            page.keyboard.press("Escape")
        # every optional number null: the block is dropped or partial, never "null"
        def prepare(fake):
            orig = fake._vccia

            def nulls(new, turbo_new):
                v = orig(new, turbo_new)
                if v:
                    v.update(margin_v=None, before_v=None, light_load=None, label=None, contributors={"P": None, "E": None, "ring": None})
                return v
            fake._vccia = nulls

        with self.session(prepare=prepare) as (page, fake):
            self.read(page)
            page.locator(".tn-rowwrap").nth(0).locator("select.tn-sel").first.select_option("-45")
            page.click("#btn-tn-plan")
            page.wait_for_selector("#dlg-plan .plan-vccia")
            self.assertNotRegex(page.inner_text("#dlg-plan .plan-vccia"), r"null|NaN|undefined")
            self.assertNotRegex(page.inner_text("#tn-vccia-card"), r"null|NaN|undefined")

    # ---- 4. units from the topology ----
    def test_six_p_cores_and_two_e_clusters_render_eight_units_everywhere(self):
        with self.session(layout=LAYOUT_6P_2E) as (page, fake):
            ids = ["P0", "P1", "P2", "P3", "P4", "P5", "EC0", "EC1"]
            # Stress: eight tiles; EC0 owns cpus 6-9
            page.click("#tab-stress")
            self.until(lambda: page.locator(".mm-tile[data-unit]").count() == 8)
            self.assertEqual(page.locator(".mm-tile[data-unit]").evaluate_all("els => els.map(e => e.dataset.unit)"), ids)
            self.assertIn("cpus 6–9", page.locator('.mm-tile[data-unit="EC0"]').inner_text())
            self.assertIn("cpus 10–13", page.locator('.mm-tile[data-unit="EC1"]').inner_text())
            self.assertEqual(page.locator('.mm-tile[aria-pressed="true"]').count(), 8)
            self.assertIn("all 8 units", page.inner_text("#st-est"))
            # Curves: eight measure chips, presets cover 6 + 2, eight lines in the chart and the legend
            page.click("#tab-curves")
            self.until(lambda: page.locator("#cv-units .unit-chip").count() == 8)
            self.assertEqual(page.locator("#cv-units .unit-chip").evaluate_all("els => els.map(e => e.dataset.unit)"), ids)
            page.wait_for_selector("#cv-chart .vc-line", timeout=10000)
            self.assertEqual(page.locator("#cv-chart .vc-line").count(), 8)
            self.assertEqual(page.locator("#cv-legend .lg-chip").count(), 8)
            # Tune: eight rows per unit, two in the simple scope
            self.read(page)
            self.assertEqual(page.locator(".tn-rowwrap").count(), 2)
            self.assertIn("6 cores", page.inner_text("#tn-rows"))
            self.assertIn("2 clusters, 8 cores", page.inner_text("#tn-rows"))
            page.click("#tn-scope button:has-text('Per unit')")
            self.assertEqual(page.locator(".tn-rowwrap").count(), 8)
            self.assertIn("cpus 6–9", page.locator(".tn-rowwrap").nth(6).inner_text())
            self.assertIn("8 curves (6 P-cores + 2 E-clusters)", page.inner_text("#tn-scope-note"))
            # edit one P-core, plan, apply: the review names the unit
            page.locator(".tn-rowwrap").nth(5).locator("select.tn-sel").first.select_option("-30")
            page.click("#btn-tn-plan")
            page.wait_for_selector("#dlg-plan[open]")
            self.assertIn("P5", page.inner_text("#dlg-plan"))
            self.until(lambda: not page.locator("#plan-go").is_disabled())
            page.click("#plan-go")
            page.wait_for_selector(".plan-result .pill.s-ok", timeout=10000)
            page.click("#btn-plan-close")
            # Profiles: the tab renders (the accepted chain is the reference list)
            page.click("#tab-profiles")
            page.wait_for_selector("#panel-profiles:not([hidden])")
            # Dashboard: the map shows the 14 simulated CPUs
            page.click("#tab-dashboard")
            self.until(lambda: page.locator("#die-cores .tile-name").count() == 14)
            self.assertEqual(page.locator("#die-cores .tile-name", has_text="E").count(), 8)

    def test_without_a_topology_the_fixed_list_stays(self):
        with self.session(prepare=lambda fake: setattr(fake, "topology_enabled", False)) as (page, fake):
            page.click("#tab-stress")
            self.assertEqual(page.locator(".mm-tile[data-unit]").count(), 12)
            page.click("#tab-curves")
            self.assertEqual(page.locator("#cv-units .unit-chip").count(), 12)


if __name__ == "__main__":
    unittest.main()
