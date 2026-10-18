"""G-208 Playwright smoke for the Tune screen (Cores grid + inspector V/F editor), mock bridge, strict CSP, zero console errors.

Run:  ARLTUNER_BROWSER_TESTS=1 python3 desktop/app/ui/tests/tune.smoke.py   (or add --browser; needs playwright for Python + google-chrome / ARLTUNER_CHROME)
Without the opt-in the browser tests are skipped. Same loopback server and CSP as shell.smoke.py (the CSP is the one of src-tauri/tauri.conf.json). No hardware, no helper:
the mock bridge replays the simulator fixtures; its baseline curve is invented (mock-backend.js says so).
"""
from __future__ import annotations

import contextlib
import functools
import http.server
import json
import os
import re
import shutil
import sys
import threading
import unittest
from pathlib import Path

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = expect = None

if "--browser" in sys.argv:
    sys.argv.remove("--browser")
    os.environ["ARLTUNER_BROWSER_TESTS"] = "1"

UI = Path(__file__).resolve().parents[1]
CSP = json.loads((UI.parent / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))["app"]["security"]["csp"]
CHROME = os.environ.get("ARLTUNER_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or ""
INIT_JS = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));
"""
P0_48_MINUS_20 = {"units": {"P0": {"pts": {"48": -20}}}, "turbo": {}}
MINUS = "\u2212"


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args) -> None:
        pass


@contextlib.contextmanager
def serve():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(UI)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def browser(test):
    return unittest.skipUnless(os.environ.get("ARLTUNER_BROWSER_TESTS") == "1" and sync_playwright and CHROME, "set ARLTUNER_BROWSER_TESTS=1 (needs playwright + chrome)")(test)


class TestStatic(unittest.TestCase):
    def test_every_tune_file_exists_and_uses_no_inline_style_or_handler(self):
        for rel in ("views/tune.js", "views/tune/grid.js", "views/tune/inspector.js", "views/tune/curve-editor.js", "views/tune/oc.js", "components/stepper.js", "styles/tune.css"):
            text = (UI / rel).read_text(encoding="utf-8")
            self.assertNotIn("innerHTML", text, rel)
            self.assertNotRegex(text, r"setAttribute\(\s*[\"']style[\"']", rel)
            self.assertNotRegex(text, r"\bon(click|input|change|keydown)\s*=", rel)


class TestTune(unittest.TestCase):
    @contextlib.contextmanager
    def page(self, query: str = "?bridge=mock&tick=100", logs: list | None = None, width: int = 1400, height: int = 1000):
        logs = logs if logs is not None else []
        with serve() as origin, sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
            try:
                page = b.new_context(viewport={"width": width, "height": height}).new_page()
                page.add_init_script(INIT_JS)
                page.on("console", lambda m: logs.append((m.type, m.text)))
                page.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
                page.on("requestfailed", lambda r: logs.append(("requestfailed", r.url)))
                page.goto(f"{origin}/index.html{query}#/tune")
                page.wait_for_selector("html[data-ready='1']", timeout=10000)
                page.wait_for_selector(".tile[data-unit='P0']", timeout=5000)
                self.logs = logs
                yield page
            finally:
                b.close()

    def assert_clean(self, page):
        self.assertEqual(page.evaluate("window.__csp"), [], "CSP violations")
        # (turbo.js is G-209's module: until it is merged the Turbo tab's dynamic import 404s and the tab says so)
        bad = [m for m in self.logs if m[0] in ("error", "pageerror", "warning", "requestfailed") and "favicon" not in m[1] and "404" not in m[1] and not m[1].endswith("/views/tune/turbo.js")]
        self.assertEqual(bad, [])

    @staticmethod
    def draft(page):
        return page.evaluate("JSON.parse(JSON.stringify(window.__arltuner.store.get().tune.draft))")

    @staticmethod
    def field(page, ratio):
        return page.locator(f".insp-section[data-kind='P'] tr[data-ratio='{ratio}'] input")

    # ---- grid and selection ----
    @browser
    def test_tiles_favored_selection_chips_and_inspector_title(self):
        with self.page() as page:
            self.assertEqual(page.locator(".tile").count(), 13)  # 8 P + 4 E clusters + ring
            self.assertEqual(page.locator(".tile[data-kind='P']").count(), 8)
            self.assertEqual(page.locator(".tile[data-kind='E']").count(), 4)
            self.assertEqual(page.locator(".tile[data-kind='R']").count(), 1)
            starred = page.locator(".tile:has(.tile-star:not([hidden]))").evaluate_all("els => els.map(e => e.dataset.unit)")
            self.assertEqual(starred, ["P0", "P3"])  # the cores with the highest 0x1C ratio
            self.assertEqual(page.inner_text(".tile[data-unit='P0'] .tile-big"), "57\u00d7")
            self.assertEqual(page.inner_text(".tile[data-unit='EC0'] .tile-big"), "48\u00d7")
            self.assertIn("cpu 8", page.inner_text(".tile[data-unit='EC0'] .tile-sub"))
            page.click(".tile[data-unit='P1']")
            self.assertEqual(page.inner_text(".insp-title"), "P-core 1")
            page.click(".tile[data-unit='P0']")
            self.assertEqual(page.inner_text(".insp-title"), "P-core 0 \u2605")
            self.assertEqual(page.get_attribute(".tile[data-unit='P0']", "aria-pressed"), "true")
            self.assertEqual(page.locator(".insp-section[data-kind='P'] tbody tr").count(), 8)
            page.click(".tile[data-unit='P2']", modifiers=["Control"])
            self.assertEqual(page.inner_text(".insp-title"), "2 P-cores")
            page.click(".tile[data-unit='P5']", modifiers=["Shift"])  # range from the anchor (P2) to P5
            self.assertEqual(page.locator(".tile[aria-pressed='true']").evaluate_all("els => els.map(e => e.dataset.unit)"), ["P2", "P3", "P4", "P5"])
            for chip, title in (("P", "All 8 P-cores"), ("E", "All 4 E-clusters"), ("favored", "2 P-cores"), ("all", "All 8 P-cores + All 4 E-clusters")):
                page.click(f"[data-chip='{chip}']")
                self.assertEqual(page.inner_text(".insp-title"), title)
                self.assertEqual(page.get_attribute(f"[data-chip='{chip}']", "aria-pressed"), "true")
            self.assertEqual(page.locator(".insp-section").count(), 2)  # P and E selected together: two sections (their points differ)
            self.assert_clean(page)

    @browser
    def test_ring_is_a_tile_with_the_same_inspector(self):
        with self.page() as page:
            page.click(".tile[data-unit='ring']")
            self.assertEqual(page.inner_text(".insp-title"), "Ring")
            self.assertEqual(page.locator(".insp-section[data-kind='R'] tbody tr").count(), 9)
            self.assertIn("limit", page.inner_text(".insp-section[data-kind='R'] .limit-line").lower())
            self.assertEqual(page.locator(".insp-section[data-kind='R'] .oc-section").count(), 0)  # the ring's OC word is never written (D-045)
            inp = page.locator(".insp-section[data-kind='R'] tr[data-key='2'] input")
            inp.click()
            page.keyboard.type("-5")
            page.keyboard.press("Enter")
            self.assertEqual(self.draft(page), {"units": {"ring": {"pts": {"2": -5}}}, "turbo": {}})
            self.assertEqual(page.inner_text("#pending-text"), "1 change on 1 unit")
            self.assertEqual(page.inner_text(".tile[data-unit='ring'] .tile-foot"), f"{MINUS}5 mV")
            self.assert_clean(page)

    # ---- one point, four ways, one draft ----
    @browser
    def test_point_4_by_typing_arrows_wheel_chart_drag_and_chart_keys_makes_the_same_draft(self):
        with self.page() as page:
            page.click(".tile[data-unit='P0']")
            self.assertEqual(self.draft(page), {"units": {}, "turbo": {}})
            field = self.field(page, 48)

            def reset():
                page.click("#pending-discard")
                self.assertEqual(self.draft(page), {"units": {}, "turbo": {}})
                self.assertTrue(page.locator("#pending-bar").is_hidden())

            def check(how):
                self.assertEqual(self.draft(page), P0_48_MINUS_20, how)
                self.assertEqual(page.inner_text("#pending-text"), "1 change on 1 unit", how)
                self.assertEqual(field.input_value(), f"{MINUS}20", how)

            # 1. typing
            field.click()
            page.keyboard.type("-20")
            page.keyboard.press("Enter")
            check("typing")
            reset()
            # 2. arrow keys (5 mV a press)
            field.focus()
            for _ in range(4):
                page.keyboard.press("ArrowDown")
            check("arrows")
            reset()
            # Shift = 25 mV, then back: Shift+Up, Down x1 (-25 + 5... no: 0 -> -25 -> -20 by ArrowUp)
            field.focus()
            page.keyboard.press("Shift+ArrowDown")
            self.assertEqual(self.draft(page)["units"]["P0"]["pts"], {"48": -25})
            page.keyboard.press("ArrowUp")
            check("shift-arrows")
            reset()
            # 3. wheel while focused (one step per notch; an unfocused field does not hijack the scroll)
            field.click()
            for _ in range(4):
                page.mouse.wheel(0, 100)
            check("wheel")
            reset()
            # 4. drag the chart marker
            marker = page.locator(".insp-section[data-kind='P'] .chart-marker[data-ratio='48']")
            chart = page.locator(".insp-section[data-kind='P'] .vf-chart")
            self.assertEqual(chart.get_attribute("data-mode"), "volt")  # the mock has an (invented) baseline curve
            mv_per_unit = float(chart.get_attribute("data-mv-per-unit"))
            view_h = float(chart.get_attribute("data-view-h"))
            rect = chart.bounding_box()
            mv_per_px = mv_per_unit * view_h / rect["height"]
            box = marker.bounding_box()
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            page.mouse.move(x, y)
            page.mouse.down()
            dy = 20 / mv_per_px
            for i in range(1, 11):
                page.mouse.move(x, y + dy * i / 10)
            page.mouse.up()
            check("chart drag")
            reset()
            # 5. chart marker with the keyboard
            marker.focus()
            for _ in range(4):
                page.keyboard.press("ArrowDown")
            check("chart keys")
            self.assert_clean(page)

    @browser
    def test_typed_values_are_clamped_by_the_policy_window_and_garbage_is_refused(self):
        with self.page() as page:
            page.click(".tile[data-unit='P0']")
            field = self.field(page, 48)
            field.click()
            page.keyboard.type("-999")
            page.keyboard.press("Enter")
            self.assertEqual(self.draft(page)["units"]["P0"]["pts"], {"48": -200})  # policy window -200..+200
            field.click()
            page.keyboard.type("abc")
            page.keyboard.press("Enter")
            self.assertEqual(field.get_attribute("aria-invalid"), "true")
            self.assertEqual(self.draft(page)["units"]["P0"]["pts"], {"48": -200})
            page.keyboard.press("Escape")
            self.assertEqual(field.input_value(), f"{MINUS}200")
            field.focus()
            page.keyboard.press("ArrowDown")  # already at the window's edge: nothing changes
            self.assertEqual(self.draft(page)["units"]["P0"]["pts"], {"48": -200})
            self.assert_clean(page)

    # ---- multi-select and mixed ----
    @browser
    def test_multi_select_all_e_shows_mixed_and_typing_sets_all(self):
        with self.page() as page:
            page.click(".tile[data-unit='EC0']")
            row15 = page.locator(".insp-section[data-kind='E'] tr[data-ratio='15'] input")
            row15.click()
            page.keyboard.type("-35")
            page.keyboard.press("Enter")
            self.assertEqual(self.draft(page), {"units": {"EC0": {"pts": {"15": -35}}}, "turbo": {}})
            page.click("[data-chip='E']")
            self.assertEqual(page.inner_text(".insp-title"), "All 4 E-clusters")
            self.assertEqual(row15.input_value(), "")
            self.assertEqual(row15.get_attribute("placeholder"), f"mixed ({MINUS}35\u20260)")
            self.assertEqual(page.locator(".insp-section[data-kind='E'] tr[data-ratio='15'] .stepper").get_attribute("data-state"), "mixed")
            other = page.locator(".insp-section[data-kind='E'] tr[data-ratio='23'] input")
            self.assertEqual(other.input_value(), "0")  # the same on all four: not mixed
            # the whole-curve field is mixed too (one point differs)
            self.assertIn("mixed", page.locator(".insp-section[data-kind='E'] .curve-top input").get_attribute("placeholder"))
            # typing sets all four
            row15.click()
            page.keyboard.type("-10")
            page.keyboard.press("Enter")
            self.assertEqual(row15.input_value(), f"{MINUS}10")
            self.assertEqual(self.draft(page), {"units": {f"EC{i}": {"pts": {"15": -10}} for i in range(4)}, "turbo": {}})
            self.assertEqual(page.inner_text("#pending-text"), "4 changes on 4 units")
            # an arrow on a mixed field steps from the pressed direction's end
            page.click("#pending-discard")
            page.click(".tile[data-unit='EC0']")
            row15.click()
            page.keyboard.type("-35")
            page.keyboard.press("Enter")
            page.click("[data-chip='E']")
            row15.focus()
            page.keyboard.press("ArrowUp")  # from the highest (0) up: all four to +5
            self.assertEqual(self.draft(page), {"units": {f"EC{i}": {"pts": {"15": 5}} for i in range(4)}, "turbo": {}})
            self.assert_clean(page)

    @browser
    def test_pending_bar_counts_units_and_changes_discard_and_review_event(self):
        with self.page() as page:
            self.assertTrue(page.locator("#pending-bar").is_hidden())
            page.click("[data-chip='P']")
            page.locator(".insp-section[data-kind='P'] .curve-top input").click()
            page.keyboard.type("-10")
            page.keyboard.press("Enter")
            # the whole curve is ONE change per unit: 8 units
            self.assertEqual(page.inner_text("#pending-text"), "8 changes on 8 units")
            page.click("[data-chip='E']")
            page.locator(".insp-section[data-kind='E'] tr[data-ratio='15'] input").click()
            page.keyboard.type("-5")
            page.keyboard.press("Enter")
            self.assertEqual(page.inner_text("#pending-text"), "12 changes on 12 units")
            self.assertEqual(page.locator(".tile.pending").count(), 12)
            # the draft survives leaving the screen and coming back
            page.click("#nav-system")
            page.click("#nav-tune")
            page.wait_for_selector(".tile[data-unit='P0']")
            self.assertEqual(page.inner_text("#pending-text"), "12 changes on 12 units")
            self.assertEqual(page.locator(".tile.pending").count(), 12)
            # Review & Apply: a request on the store (G-210 builds the drawer on it)
            page.evaluate("window.__reviews = []; window.__arltuner.store.events.addEventListener('tune:review', (e) => window.__reviews.push(e.detail))")
            page.click("#pending-review")
            self.assertEqual(page.evaluate("window.__reviews"), [{"changes": 12, "units": 12}])
            self.assertEqual(page.evaluate("window.__arltuner.store.get().tune.reviewSeq"), 1)
            page.keyboard.press("Escape")
            page.click("#pending-discard")
            self.assertEqual(self.draft(page), {"units": {}, "turbo": {}})
            self.assertEqual(page.locator(".tile.pending").count(), 0)
            self.assertTrue(page.locator("#pending-bar").is_hidden())
            self.assert_clean(page)

    # ---- limits, predictions, OC ----
    @browser
    def test_limit_hints_name_the_cpu_source(self):
        # D-049 (G-217): the inspector shows the CPU's own limits, never the operator's policy
        with self.page() as page:
            page.click(".tile[data-unit='P0']")
            line = page.inner_text(".insp-section[data-kind='P'] .curve-editor > .limit-line")
            self.assertIn("CPU limit", line)
            self.assertIn(f"{MINUS}1000\u2026+999 mV", line)
            self.assertNotIn("policy", line)
            cell = page.locator(".insp-section[data-kind='P'] tr[data-ratio='48'] .c-lim")
            self.assertIn("CPU", cell.inner_text())
            self.assertNotIn("policy", cell.inner_text() + (cell.get_attribute("title") or ""))
            oc = page.inner_text(".oc-section .limit-line")
            self.assertIn("CPU limit", oc)
            self.assertNotIn("policy", oc)
            self.assert_clean(page)

    @browser
    def test_predicted_voltage_follows_the_offset_on_the_baseline_and_shows_the_clamp(self):
        with self.page() as page:
            page.click(".tile[data-unit='P0']")
            row = page.locator(".insp-section[data-kind='P'] tr[data-ratio='48']")
            base = row.locator(".c-num").nth(0).inner_text()
            self.assertRegex(base, r"^\d\.\d{3} V$")
            self.assertEqual(row.locator(".c-num").nth(1).inner_text(), base)  # no offset: predicted == base
            self.field(page, 48).click()
            page.keyboard.type("-20")
            page.keyboard.press("Enter")
            pred = float(row.locator(".c-num").nth(1).inner_text().split()[0])
            self.assertAlmostEqual(pred, float(base.split()[0]) - 0.020, places=3)
            self.assertEqual(row.locator(".c-num").nth(2).inner_text(), base)  # read-back is the CPU's, unchanged
            # point 5 (53x) lowered by far more than point 4: its voltage is raised to point 4's, and the row says so
            page.locator(".insp-section[data-kind='P'] tr[data-ratio='53'] input").click()
            page.keyboard.type("-200")
            page.keyboard.press("Enter")
            clamped = page.locator(".insp-section[data-kind='P'] tr[data-ratio='53'] .c-num").nth(1)
            self.assertTrue(clamped.inner_text().endswith("\u2191"))
            self.assertIn("V(n)", clamped.get_attribute("title"))
            self.assert_clean(page)

    @browser
    def test_without_a_baseline_curve_only_the_fused_top_has_a_voltage_and_the_reason_is_shown(self):
        with self.page("?bridge=mock&tick=100&baseline=none") as page:
            page.click(".tile[data-unit='P0']")
            expect(page.locator(".insp-section[data-kind='P'] .base-note")).to_contain_text("Curves \u2192 Measure", timeout=5000)
            row4 = page.locator(".insp-section[data-kind='P'] tr[data-ratio='48']")
            self.assertEqual(row4.locator(".c-num").nth(1).inner_text(), "\u2013")
            self.assertIn("no baseline curve (Curves \u2192 Measure)", row4.locator(".c-num").nth(1).get_attribute("title"))
            top = page.locator(".insp-section[data-kind='P'] tr[data-ratio='57'] .c-num")
            self.assertEqual(top.nth(1).inner_text(), "1.426 V")  # the fused top point (cmd 0x09)
            self.assertEqual(page.locator(".insp-section[data-kind='P'] .vf-chart").get_attribute("data-mode"), "mv")  # offsets only
            self.assert_clean(page)

    @browser
    def test_oc_section_ratio_target_and_no_oc_point_wording(self):
        with self.page() as page:
            page.click(".tile[data-unit='EC0']")
            info = page.inner_text(".insp-section[data-kind='E'] .oc-info")
            self.assertIn("48\u00d7", info)
            self.assertIn("fused top 46\u00d7", info)
            self.assertIn("no OC point", info)
            target = page.locator(".insp-section[data-kind='E'] .oc-section input[aria-label$='adaptive target voltage']")
            self.assertTrue(target.is_disabled())
            self.assertIn("fused top", target.get_attribute("title"))
            ratio = page.locator(".insp-section[data-kind='E'] .oc-section input[aria-label$='OC ratio']")
            ratio.click()
            page.keyboard.type("49")
            page.keyboard.press("Enter")
            d = self.draft(page)["units"]["EC0"]
            self.assertEqual(d["ratio"], 49)
            self.assertTrue(d["vtAuto"] and d["vt"] > 0.9)  # an OC point needs a target: the draft takes the fused top voltage
            self.assertFalse(target.is_disabled())
            self.assertIn("OC point 7", page.inner_text(".insp-section[data-kind='E'] .oc-info"))
            self.assertEqual(page.inner_text("#pending-text"), "2 changes on 1 unit")
            target.focus()
            page.keyboard.press("ArrowUp")
            self.assertGreater(self.draft(page)["units"]["EC0"]["vt"], d["vt"])
            page.click(".insp-section[data-kind='E'] .oc-section button:has-text('Back to read')")
            self.assertEqual(self.draft(page), {"units": {}, "turbo": {}})
            self.assert_clean(page)

    # ---- keyboard only ----
    @browser
    def test_keyboard_only_path_from_the_tile_grid_to_an_edited_point(self):
        with self.page() as page:
            page.focus(".tile[data-unit='P0']")  # where Tab lands: the roving stop of the grid
            page.keyboard.press("ArrowRight")
            self.assertEqual(page.evaluate("document.activeElement.dataset.unit"), "P1")
            page.keyboard.press("ArrowDown")  # next row of tiles
            self.assertEqual(page.evaluate("document.activeElement.dataset.unit"), "P5")
            page.keyboard.press("ArrowUp")
            page.keyboard.press("ArrowLeft")
            self.assertEqual(page.evaluate("document.activeElement.dataset.unit"), "P0")
            page.keyboard.press("ArrowRight")
            page.keyboard.press("Enter")  # select P1
            self.assertEqual(page.inner_text(".insp-title"), "P-core 1")
            page.keyboard.press("ArrowRight")
            page.keyboard.press("Control+Space")  # add P2
            self.assertEqual(page.inner_text(".insp-title"), "2 P-cores")
            page.keyboard.press("ArrowLeft")
            page.keyboard.press("Control+Space")  # remove P1 again
            page.keyboard.press("ArrowLeft")
            page.keyboard.press("Enter")
            page.keyboard.press("ArrowRight")
            page.keyboard.press("ArrowLeft")
            page.keyboard.press("Enter")  # P0 only
            self.assertEqual(page.inner_text(".insp-title"), "P-core 0 \u2605")
            for _ in range(40):  # Tab through the inspector to the point-4 field
                page.keyboard.press("Tab")
                if page.evaluate("document.activeElement.closest('tr') && document.activeElement.closest('tr').dataset.ratio") == "48":
                    break
            self.assertEqual(page.evaluate("document.activeElement.closest('tr').dataset.ratio"), "48")
            page.keyboard.press("ArrowDown")  # -5
            page.keyboard.press("ArrowDown")  # -10
            page.keyboard.press("Shift+ArrowDown")  # the next multiple of 25 below: -25
            page.keyboard.press("ArrowUp")  # -20
            self.assertEqual(self.draft(page), P0_48_MINUS_20)
            # and out of the field again with the keyboard: Tab to the next point, type, Enter
            page.keyboard.press("Tab")
            self.assertEqual(page.evaluate("document.activeElement.closest('tr').dataset.ratio"), "53")
            page.keyboard.type("-15")
            page.keyboard.press("Enter")
            self.assertEqual(self.draft(page)["units"]["P0"]["pts"], {"48": -20, "53": -15})
            self.assert_clean(page)

    # ---- read, tabs, live ----
    @browser
    def test_read_cpu_state_shows_progress_and_the_measured_duration(self):
        with self.page() as page:
            page.click(".tile[data-unit='P0']")
            self.field(page, 48).click()
            page.keyboard.type("-20")
            page.keyboard.press("Enter")
            self.assertEqual(page.inner_text("#pending-text"), "1 change on 1 unit")
            page.click("#tune-read")
            expect(page.locator("#tune-read-status")).to_have_text(re.compile(r"^Reading"), timeout=3000)
            self.assertTrue(page.is_disabled("#tune-read"))
            self.assertTrue(page.locator(".read-progress").is_visible())
            expect(page.locator("#tune-read-status")).to_have_text(re.compile(r"^Read at"), timeout=15000)
            status = page.inner_text("#tune-read-status")
            self.assertRegex(status, r"^Read at \d\d:\d\d:\d\d \u00b7 \d+ ops? in \d+(\.\d)? ?(s|ms)$")
            self.assertRegex(page.inner_text("#tune-read"), r"^Re-read CPU state \(~\d+(\.\d)? ?(s|ms)\)$")
            self.assertEqual(self.draft(page), {"units": {}, "turbo": {}})  # a new read replaces what the draft was made against
            self.assertTrue(page.locator("#pending-bar").is_hidden())
            self.assertEqual(page.inner_text(".insp-title"), "P-core 0 \u2605")
            self.assertEqual(self.field(page, 53).input_value(), "0")  # the recorded tune.read, not the applied one of tune.last
            self.assert_clean(page)

    @browser
    def test_sub_tabs_placeholders_and_keyboard(self):
        with self.page() as page:
            tabs = page.locator("[role='tab']")
            self.assertEqual(tabs.all_inner_texts(), ["Cores", "Turbo ratios", "Power", "Ring & uncore", "Ratio caps"])  # "Ratios & parking" minus parking (D-048), G-217
            self.assertEqual(page.get_attribute("#subtab-cores", "aria-selected"), "true")
            page.focus("#subtab-cores")
            page.keyboard.press("ArrowRight")  # Turbo ratios (G-209: its own module; a stand-in text while it is not there)
            self.assertEqual(page.get_attribute("#subtab-turbo", "aria-selected"), "true")
            expect(page.locator("#tune-panel")).not_to_have_text(re.compile(r"Loading|^\s*$"), timeout=5000)
            page.keyboard.press("ArrowRight")
            page.wait_for_selector("#tune-panel .rt-table", timeout=5000)  # G-217: the Power tab (views/tune/power.js)
            self.assertIn("Processor Core Power Limit (PL1)", page.inner_text("#tune-panel"))
            page.keyboard.press("End")
            self.assertEqual(page.get_attribute("#subtab-ratios", "aria-selected"), "true")
            page.wait_for_selector("#tune-panel .rt-table tr[data-unit='P0']", timeout=5000)
            page.click("#subtab-cores")
            page.wait_for_selector(".tile[data-unit='P0']")
            self.assert_clean(page)

    @browser
    def test_layout_has_no_horizontal_overflow_from_the_minimum_window_up(self):
        for width, height in ((960, 600), (1100, 800), (1280, 800)):
            with self.page(width=width, height=height) as page:
                page.click("[data-chip='all']")
                self.assertFalse(page.evaluate("document.getElementById('screen').scrollWidth > document.getElementById('screen').clientWidth"), f"{width}px")
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > window.innerWidth"), f"{width}px")
                page.click(".tile[data-unit='ring']")
                self.assertFalse(page.evaluate("document.getElementById('screen').scrollWidth > document.getElementById('screen').clientWidth"), f"{width}px ring")
                self.assert_clean(page)

    @browser
    def test_monitors_mode_shows_live_values_or_n_a(self):
        with self.page("?bridge=mock&motion=1&tick=50") as page:
            page.click(".seg-btn:has-text('Monitors')")
            expect(page.locator(".tile[data-unit='P0'] .tile-big")).to_have_text(re.compile(r"^\d\.\d\d GHz$"), timeout=5000)
            self.assertRegex(page.inner_text(".tile[data-unit='P0'] .tile-foot"), r"^temp n/a \u00b7 \d\.\d{3} V$")  # no per-core temperature in the telemetry: said, not invented
            page.click(".seg-btn:has-text('Ratio')")
            self.assertEqual(page.inner_text(".tile[data-unit='P0'] .tile-big"), "57\u00d7")
            self.assert_clean(page)


if __name__ == "__main__":
    unittest.main()
