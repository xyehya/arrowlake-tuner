"""G-127 (D-036): the Tune page shows live data only. Plain names, the provenance of the shown read, the favored star from the preflight topology or the 0x1C read
(never a built-in P0 / P3), no prediction above a core's reach, and no reference-machine lore in the shipped page.

Static checks (always run) plus an opt-in browser check (``ARLTUNER_BROWSER_TESTS=1``, Playwright + Chrome) against the contract-9 fake helper, whose favored set,
``topology`` and ``read_at`` are switchable. No hardware, no real helper code."""
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
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")
CSS = (WEB / "style.css").read_text(encoding="utf-8")
CHROME = os.environ.get("ARLTUNER_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or ""
STAR = "★"


class TestStatic(unittest.TestCase):
    def test_no_lore_strings_in_the_shipped_page(self):
        both = HTML + JS
        for needle in ("tuning words", "Tuning words", "this machine", "(BIOS)", '"P0", "P3"', "const FAVORED", "OCCT", "COST_LOW", "COST_HIGH", "124&#8211;136"):
            self.assertNotIn(needle, both, needle)
        self.assertNotRegex(JS, r"\bFAVORED\b")
        self.assertNotRegex(JS, r"VCCIA|ring: 1\.2")

    def test_plain_button_names(self):
        self.assertIn(">Read ratios &amp; voltages</button>", HTML)
        self.assertIn('"Re-read ratios & voltages"', JS)

    def test_favored_comes_from_the_topology_then_the_read(self):
        self.assertIn("r.topology", JS)
        self.assertIn("favored_source", JS)
        self.assertIn("turbo.favored", JS)
        self.assertIn("favored core (highest CPPC performance)", JS)
        self.assertIn("favored core (0x1C favored ratio)", JS)
        self.assertIn("if (FAV.ids.length)", JS)          # the Favored preset exists only when cores were detected

    def test_predictions_are_labelled_and_need_a_baseline(self):
        self.assertIn("Measure a stock curve (Curves) to see predicted voltages", JS)
        self.assertIn("They are not read from the CPU", JS)
        self.assertIn('id="tn-pred-note"', HTML)
        self.assertIn('id="tn-prov"', HTML)

    def test_provenance_wording(self):
        for text in ("Read from the CPU", "Showing the helper\\u2019s earlier read", "(time unknown)", "no mailbox operations used"):
            self.assertIn(text, JS, text)


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

    @contextlib.contextmanager
    def session(self, *, favored=None, topology=True, read_at=True, seed=True, stored=False, set_topology=None):
        from tests.web import v3_harness as h

        srv, fake, stop = h.start(seed=seed, apply_delay=0.3)   # a slower apply: the page learns the op id from the HTTP answer before the result event arrives
        if favored is not None:
            fake.favored = favored
        fake.topology_enabled = topology
        if set_topology is not None:   # the web server keeps the first preflight answer, so what the topology says is fixed before the page loads
            fake.set_topology(**set_topology)
        fake.read_at_enabled = read_at
        if stored:
            fake.seed_last_read(time.time() - 3 * 60)
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
        page.click("#tn-scope button:has-text('Per unit')")

    def test_names_provenance_and_no_lore(self):
        with self.session(read_at=True, seed=False) as (page, fake):
            page.click("#tab-tune")
            self.assertIn("Read ratios & voltages", page.inner_text("#btn-tune-read"))
            self.read(page)
            self.assertIn("Re-read ratios & voltages", page.inner_text("#btn-tune-read"))
            self.assertRegex(page.inner_text("#tn-prov"), r"^Read from the CPU at \d\d?:\d\d.*\(just now\)")
            text = page.locator("body").text_content()
            for needle in ("tuning words", "Tuning words", "this machine", "(BIOS)"):
                self.assertNotIn(needle, text, needle)
            # after an apply the line says it follows the read-back, not a fresh read
            page.locator(".tn-rowwrap").nth(0).locator("select.tn-sel").first.select_option("-45")
            page.click("#btn-tn-plan")
            page.wait_for_selector("#dlg-plan[open]")
            self.until(lambda: not page.locator("#plan-go").is_disabled())
            page.click("#plan-go")
            page.wait_for_selector(".plan-result .pill.s-ok", timeout=10000)
            self.assertIn("Updated from the apply’s read-back", page.inner_text("#tn-prov"))

    def test_a_stored_read_is_labelled_and_a_missing_time_says_so(self):
        with self.session(stored=True) as (page, fake):
            page.click("#tab-tune")
            page.wait_for_selector("#tn-console:not([hidden])", timeout=8000)
            prov = page.inner_text("#tn-prov")
            self.assertIn("Showing the helper’s earlier read from", prov)
            self.assertRegex(prov, r"\(\d+ min ago\)")
            self.assertIn("no mailbox operations used", prov)
        with self.session(read_at=False) as (page, fake):
            self.read(page)
            self.assertIn("Read from the CPU (time unknown)", page.inner_text("#tn-prov"))

    def test_favored_star_follows_the_detected_cores_only(self):
        with self.session() as (page, fake):   # the fake's default: P0 and P3
            self.read(page)
            self.assertEqual([t for t in page.locator(".tn-rowwrap .tn-name").all_inner_texts() if STAR in t], ["P-core 0 " + STAR, "P-core 3 " + STAR])
            self.assertIn("highest CPPC performance", page.locator(".tn-rowwrap .tn-name", has_text=STAR).first.get_attribute("title"))
            page.click("#tab-stress")
            self.assertEqual([t for t in page.locator(".mm-tile[data-unit] .mm-name").all_inner_texts() if STAR in t], ["P0 " + STAR, "P3 " + STAR])
            page.click("#tab-curves")
            self.assertEqual([t for t in page.locator("#cv-units .uc-name").all_inner_texts() if STAR in t], ["P0 " + STAR, "P3 " + STAR])
            self.assertEqual([t for t in page.locator("#cv-legend .lg-chip").all_inner_texts() if STAR in t], ["P0 " + STAR, "P3 " + STAR])
            self.assertIn("P0, P3", page.locator("#cv-presets button", has_text="Favored").get_attribute("title"))
            page.click("#tab-dashboard")
            self.until(lambda: page.locator("#die-cores .tile-name", has_text=STAR).count() == 2)

    def test_favored_is_only_the_detected_core_not_p0_and_p3(self):
        with self.session(favored=("P2",)) as (page, fake):
            self.read(page)
            self.assertEqual([t for t in page.locator(".tn-rowwrap .tn-name").all_inner_texts() if STAR in t], ["P-core 2 " + STAR])
            page.click("#tab-stress")
            self.assertEqual([t for t in page.locator(".mm-tile[data-unit] .mm-name").all_inner_texts() if STAR in t], ["P2 " + STAR])
            page.click("#tab-curves")
            self.assertIn("P2", page.locator("#cv-presets button", has_text="Favored").get_attribute("title"))
            self.assertNotIn("P0", page.locator("#cv-presets button", has_text="Favored").get_attribute("title"))
            # P0 is no longer favored: its 56x and 57x points show the reach caption only, no predicted voltage
            page.click("#tab-tune")
            page.locator(".tn-rowwrap").nth(0).locator(".seg button:has-text('Per point')").click()
            pts = page.locator("#tn-pts-P0 .tn-pt.unreach")
            self.assertEqual(pts.count(), 2)
            for i in range(2):
                self.assertIn("not reachable on this core, tops at 55×", pts.nth(i).inner_text())
                self.assertNotIn("→", pts.nth(i).inner_text())
                self.assertNotIn("(fused)", pts.nth(i).inner_text())

    def test_no_favored_cores_means_no_star_and_no_preset(self):
        with self.session(favored=()) as (page, fake):
            self.read(page)
            self.assertEqual([t for t in page.locator(".tn-rowwrap .tn-name").all_inner_texts() if STAR in t], [])
            page.click("#tab-curves")
            self.assertEqual(page.locator("#cv-presets button").count(), 3)   # All, P-cores, E-clusters
            self.assertEqual(page.locator("#cv-presets button", has_text="Favored").count(), 0)
            page.click("#tab-stress")
            self.assertEqual(page.locator(".mm-tile[data-unit] .mm-name", has_text=STAR).count(), 0)

    def test_without_a_topology_the_star_waits_for_the_read(self):
        with self.session(topology=False) as (page, fake):
            page.click("#tab-stress")
            self.assertEqual(page.locator(".mm-tile[data-unit] .mm-name", has_text=STAR).count(), 0)   # nothing is favored before anything was read
            self.read(page)
            self.assertIn("0x1C favored ratio", page.locator(".tn-rowwrap .tn-name", has_text=STAR).first.get_attribute("title"))
            page.click("#tab-stress")
            self.assertEqual([t for t in page.locator(".mm-tile[data-unit] .mm-name").all_inner_texts() if STAR in t], ["P0 " + STAR, "P3 " + STAR])

    def test_the_system_saying_none_favored_is_not_overridden_by_the_read(self):
        """Topology present with a source but no favored core = every P-core judged equal: no star, even though the read's 0x1C ratios differ (and the page says so)."""
        with self.session(set_topology={"source": "acpi_cppc", "favored": ()}) as (page, fake):
            self.read(page)
            self.assertEqual([t for t in page.locator(".tn-rowwrap .tn-name").all_inner_texts() if STAR in t], [])
            self.assertIn("favored cores named by the system differ", page.inner_text("#tn-prov"))

    def test_a_topology_without_any_source_falls_back_to_the_read(self):
        with self.session(set_topology={"source": None, "favored": ()}) as (page, fake):
            self.read(page)
            self.assertEqual(len([t for t in page.locator(".tn-rowwrap .tn-name").all_inner_texts() if STAR in t]), 2)
            self.assertIn("0x1C favored ratio", page.locator(".tn-rowwrap .tn-name", has_text=STAR).first.get_attribute("title"))

    def test_a_non_favored_core_shows_reach_only_for_56_and_57(self):
        with self.session() as (page, fake):
            self.read(page)
            page.locator(".tn-rowwrap").nth(1).locator(".seg button:has-text('Per point')").click()
            pts = page.locator("#tn-pts-P1 .tn-pt.unreach")
            self.assertEqual(pts.count(), 2)
            for i, ratio in enumerate((56, 57)):
                t = pts.nth(i).inner_text()
                self.assertIn(f"{ratio}×", t)
                self.assertIn("not reachable on this core, tops at 55×", t)
                self.assertNotIn("→", t)
                self.assertNotIn("fused", t.replace("fused · offset only", ""))

    def test_predictions_are_labelled_and_absent_without_a_baseline(self):
        with self.session(seed=False) as (page, fake):
            self.read(page)
            self.assertIn("Measure a stock curve (Curves) to see predicted voltages", page.inner_text("#tn-pred-note"))
            page.locator(".tn-rowwrap").nth(0).locator(".seg button:has-text('Per point')").click()
            self.assertNotIn("→", page.inner_text("#tn-pts-P0"))
            self.assertNotIn("needs a baseline", page.inner_text("#tn-pts-P0"))
        with self.session(seed=True) as (page, fake):
            self.read(page)
            note = page.inner_text("#tn-pred-note")
            self.assertIn("baseline ‘Stock baseline’ measured 2026-10-07", note)
            self.assertIn("not read from the CPU", note)

    def test_the_op_cost_comes_from_the_read(self):
        with self.session() as (page, fake):
            page.click("#tab-tune")
            self.assertIn("about one operation per V/F point", page.inner_text("#tn-cost"))
            self.read(page)
            self.assertIn("about 112 operations", page.inner_text("#tn-cost"))
            self.assertNotRegex(page.inner_text("#tn-cost"), r"124|136")

    def test_the_vid_card_has_no_fixed_ring_or_headroom(self):
        with self.session() as (page, fake):
            self.read(page)
            card = page.inner_text("#tn-vccia-card")
            self.assertIn("Highest predicted VID", card)
            for needle in ("Ring", "headroom", "40×"):
                self.assertNotIn(needle, card, needle)
            self.until(lambda: "Board Vcore sensor now" in page.inner_text("#tn-vccia-live"))


if __name__ == "__main__":
    unittest.main()
