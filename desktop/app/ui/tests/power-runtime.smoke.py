"""G-217 Playwright smoke for the Power, Ring & uncore and Ratio caps sub-tabs of Tune, mock bridge, strict CSP, zero console errors.

Run:  ARLTUNER_BROWSER_TESTS=1 /usr/bin/python3 desktop/app/ui/tests/power-runtime.smoke.py   (or add --browser; needs playwright for Python + google-chrome / ARLTUNER_CHROME)
Without the opt-in the browser tests are skipped. Same loopback server and CSP as tune.smoke.py. No hardware, no helper: the mock bridge answers power.* / runtime.* from the recorded simulator
shapes (mock-backend-g217.js: cpu_limits is recorded too; only the state that follows what is asked is invented there, marked). Knobs: `&cpulimits=unread`, `&offline=<cpu>`, `&rtapply=mismatch`, `&locked=1`.
The drawer tests run the real apply drawer (views/tune/apply.js planArgs / view / explain hooks): plan, review rows, amber plan warnings, apply, read-back, revert.
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
CAPTURE_JS = """
() => {
  window.__reviews = [];
  window.__arltuner.store.events.addEventListener('tune:review', (e) => window.__reviews.push(e.detail));
}
"""


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
    def test_files_exist_and_use_no_inline_style_or_handler(self):
        for rel in ("views/tune/power.js", "views/tune/ring.js", "views/tune/runtime.js", "views/tune/runtime-kinds.js", "views/tune/rt-common.js", "lib/cpulimits.js", "lib/rtstate.js", "styles/runtime.css"):
            text = (UI / rel).read_text(encoding="utf-8")
            self.assertNotIn("innerHTML", text, rel)
            self.assertNotRegex(text, r"setAttribute\(\s*[\"']style[\"']", rel)
            self.assertNotRegex(text, r"\bon(click|input|change|keydown)\s*=", rel)

    def test_no_policy_words_in_the_new_views(self):
        for rel in ("views/tune/power.js", "views/tune/ring.js", "views/tune/runtime.js", "views/tune/rt-common.js", "lib/cpulimits.js"):
            text = (UI / rel).read_text(encoding="utf-8")
            body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//") and not line.lstrip().startswith("*"))
            self.assertNotRegex(body.replace("D-049", ""), r"policy\.toml|your policy|from your policy", rel)


class TestTabs(unittest.TestCase):
    @contextlib.contextmanager
    def page(self, tab: str, query: str = "", width: int = 1400, height: int = 1000):
        logs: list = []
        with serve() as origin, sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
            try:
                page = b.new_context(viewport={"width": width, "height": height}).new_page()
                page.add_init_script(INIT_JS)
                page.on("console", lambda m: logs.append((m.type, m.text)))
                page.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
                page.on("requestfailed", lambda r: logs.append(("requestfailed", r.url)))
                page.goto(f"{origin}/index.html?bridge=mock&tick=100{query}#/tune")
                page.wait_for_selector("html[data-ready='1']", timeout=10000)
                page.wait_for_selector(".tile[data-unit='P0']", timeout=5000)
                page.evaluate(CAPTURE_JS)
                page.click(f"#subtab-{tab}")
                if tab in ("power", "ring", "ratios"):
                    page.wait_for_selector(".rt-read-text[data-state='ready']", timeout=5000)  # power.read / runtime.read answered
                self.logs = logs
                yield page
            finally:
                b.close()

    def assert_clean(self, page):
        self.assertEqual(page.evaluate("window.__csp"), [], "CSP violations")
        bad = [m for m in self.logs if m[0] in ("error", "pageerror", "warning", "requestfailed") and "favicon" not in m[1] and "404" not in m[1]]
        self.assertEqual(bad, [])

    @staticmethod
    def edit(page, label: str, value: str):
        field = page.get_by_label(label, exact=True)
        field.fill(value)
        field.press("Enter")

    @staticmethod
    def type_point(page, ratio: int, value: str):
        field = page.locator(f".insp-section[data-kind='P'] tr[data-ratio='{ratio}'] input")
        field.click()
        page.keyboard.press("Control+A")
        page.keyboard.type(value)
        page.keyboard.press("Enter")

    # ---- Power ----
    @browser
    def test_power_tab_shows_xtu_labels_current_values_and_lost_on_reboot(self):
        with self.page("power") as page:
            page.wait_for_selector(".rt-table tr[data-key='pl1_w']")
            self.assertEqual(page.locator(".rt-table tbody tr").count(), 3)
            names = page.locator(".rt-table tbody th").all_inner_texts()
            self.assertEqual(names, ["Processor Core Power Limit (PL1)", "Processor Core Turbo Power Limit (PL2)", "Turbo Boost Power Time Window"])
            self.assertIn("lost on reboot", page.inner_text(".rt-chip"))
            self.assertIn("250 W · 250 W", page.inner_text("tr[data-key='pl1_w'] .rt-now"))
            self.assertIn("PL field maximum", page.inner_text(".rt-info"))
            self.assertNotRegex(page.inner_text("#tune-panel"), r"policy")
            self.assertEqual(page.inner_text("tr[data-key='pl1_w'] td:last-child"), "not changed yet")
            self.assertTrue(page.locator("#rt-revert").is_disabled())
            self.assertTrue(page.locator("#rt-review").is_disabled())
            self.assert_clean(page)

    @browser
    def test_power_edit_is_pending_pl2_below_pl1_warns_and_review_dispatches_the_helper_shaped_request(self):
        with self.page("power") as page:
            page.wait_for_selector(".rt-table tr[data-key='pl1_w']")
            self.edit(page, "Processor Core Power Limit (PL1)", "230")
            self.assertEqual(page.inner_text(".rt-pending"), "1 pending runtime change")
            expect(page.locator("tr[data-key='pl1_w']")).to_have_class(re.compile(r"\bpending\b"))
            self.assertIn("was 250 W", page.inner_text("tr[data-key='pl1_w'] .step-was"))
            self.edit(page, "Processor Core Turbo Power Limit (PL2)", "200")
            self.assertEqual(page.locator(".rt-warns li[data-code='PL2_BELOW_PL1']").count(), 1)
            self.assertFalse(page.locator("#rt-review").is_disabled(), "a warning never blocks")
            self.assertEqual(page.inner_text(".rt-pending"), "2 pending runtime changes")
            page.fill("input[aria-label='Processor Core Power Limit (PL1)']", "251")  # not a commit yet: Escape drops the text
            page.press("input[aria-label='Processor Core Power Limit (PL1)']", "Escape")
            page.click("#rt-discard")
            self.assertEqual(page.inner_text(".rt-pending"), "No pending changes")
            self.edit(page, "Processor Core Power Limit (PL1)", "230")
            page.click("#rt-review")
            rv = page.evaluate("window.__reviews[0]")
            self.assertEqual(rv["requests"], [{"kind": "power", "intent": {"power": {"pl1_w": 230}}, "label": "Power limits"}])
            self.assert_clean(page)

    @browser
    def test_power_whole_watts_only_and_unknown_text_is_refused_in_place(self):
        with self.page("power") as page:
            page.wait_for_selector(".rt-table tr[data-key='pl1_w']")
            field = page.get_by_label("Processor Core Power Limit (PL1)", exact=True)
            field.fill("230.5")
            field.press("Enter")
            self.assertEqual(field.get_attribute("aria-invalid"), "true")
            self.assertEqual(page.inner_text(".rt-pending"), "No pending changes")
            self.assert_clean(page)

    @browser
    def test_power_is_disabled_until_the_cpu_limits_are_read_and_when_locked(self):
        with self.page("power", "&cpulimits=unread") as page:
            page.wait_for_selector(".rt-table tr[data-key='pl1_w']")
            self.assertTrue(page.get_by_label("Processor Core Power Limit (PL1)", exact=True).is_disabled())
            self.assertIn("Read CPU state first", page.inner_text(".rt-info"))
            self.assert_clean(page)
        with self.page("power", "&locked=1") as page:
            page.wait_for_selector(".rt-table tr[data-key='pl1_w']")
            self.assertTrue(page.get_by_label("Processor Core Power Limit (PL1)", exact=True).is_disabled())
            self.assertIn("locked", page.inner_text(".rt-info"))
            self.assert_clean(page)

    @browser
    def test_power_review_apply_revert_through_the_drawer(self):
        with self.page("power") as page:
            page.wait_for_selector(".rt-table tr[data-key='pl1_w']")
            self.edit(page, "Processor Core Power Limit (PL1)", "230")
            self.edit(page, "Turbo Boost Power Time Window", "28")
            page.click("#rt-review")
            page.wait_for_selector("#apply-go", timeout=5000)
            sec = page.locator(".apply-section[data-kind='power']")
            self.assertIn("250 W → 230 W", sec.inner_text())
            self.assertIn("Processor Core Power Limit (PL1)", sec.inner_text())
            self.assertEqual(page.inner_text("#apply-go"), "Apply 2 writes")
            page.click("#apply-go")
            page.wait_for_selector(".apply-result", timeout=8000)
            self.assertIn("read back as written", page.inner_text(".apply-headline"))
            page.click("#apply-close")
            page.wait_for_selector("tr[data-key='pl1_w'] td:last-child:has-text('250 W')")
            self.assertIn("230 W · 230 W", page.inner_text("tr[data-key='pl1_w'] .rt-now"))
            self.assertEqual(page.inner_text(".rt-pending"), "No pending changes")
            self.assertFalse(page.locator("#rt-revert").is_disabled())
            page.click("#rt-revert")
            page.wait_for_selector("#apply-go")
            self.assertIn("revert to the values of the boot", page.inner_text(".apply-section[data-kind='power'] h3"))
            page.click("#apply-go")
            page.wait_for_selector(".apply-result", timeout=8000)
            page.click("#apply-close")
            page.wait_for_selector("tr[data-key='pl1_w'] .rt-now:has-text('250 W · 250 W')")
            self.assert_clean(page)

    @browser
    def test_drawer_shows_the_plan_warnings_amber_and_a_refusal_names_the_cpu_rule(self):
        with self.page("power") as page:
            page.wait_for_selector(".rt-table tr[data-key='pl1_w']")
            self.edit(page, "Processor Core Turbo Power Limit (PL2)", "200")
            page.click("#rt-review")
            page.wait_for_selector("#apply-go")
            warn = page.locator(".apply-warnings li[data-code='PL2_BELOW_PL1']")
            self.assertEqual(warn.count(), 1)
            self.assertIn("warn", warn.get_attribute("class"))
            self.assertFalse(page.locator("#apply-go").is_disabled(), "warnings never block Apply")
            page.click("#apply-close")
            self.edit(page, "Processor Core Power Limit (PL1)", "5000")
            page.click("#rt-review")
            page.wait_for_selector("#apply-plan-error")
            text = page.inner_text("#apply-plan-error")
            self.assertIn("5000", text)
            self.assertIn("MSR 0x610", text)
            self.assertNotRegex(text, r"policy")
            self.assert_clean(page)

    # ---- Ring & uncore ----
    @browser
    def test_ring_tab_has_the_ring_curve_editor_and_the_uncore_range_within_the_cpu_window(self):
        with self.page("ring") as page:
            page.wait_for_selector(".rt-section[data-section='uncore'] tr[data-key='max_ratio']")
            self.assertEqual(page.locator(".rt-section[data-section='ring-vf'] table.vf-table").count(), 1, "the ring V/F editor of the Cores inspector")
            self.assertGreaterEqual(page.locator(".rt-section[data-section='ring-vf'] tr[data-ring='1']").count(), 9)
            self.assertIn("lost on reboot", page.inner_text(".rt-chip"))
            self.assertEqual(page.inner_text("tr[data-key='max_ratio'] .rt-now"), "40×")
            self.assertIn("uncore 8×–40×", page.inner_text(".rt-info"))
            self.edit(page, "Uncore maximum ratio", "38")
            self.assertEqual(page.inner_text(".rt-pending"), "1 pending runtime change")
            self.edit(page, "Uncore maximum ratio", "60")  # clamped into the CPU window
            self.assertEqual(page.get_by_label("Uncore maximum ratio", exact=True).input_value(), "40")
            self.edit(page, "Uncore minimum ratio", "39")
            self.edit(page, "Uncore maximum ratio", "38")
            self.assertEqual(page.locator(".rt-warns li[data-code='UNCORE_MIN_ABOVE_MAX']").count(), 1)
            page.click("#rt-discard")
            self.edit(page, "Uncore maximum ratio", "38")
            page.click("#rt-review")
            self.assertEqual(page.evaluate("window.__reviews[0].requests"), [{"kind": "runtime", "intent": {"uncore": {"max_ratio": 38}}, "label": "Ratio caps & uncore"}])
            self.assert_clean(page)

    @browser
    def test_ring_vf_edit_rides_along_in_the_same_review(self):
        with self.page("ring") as page:
            page.wait_for_selector(".rt-section[data-section='ring-vf'] tr[data-ring='1']")
            row = page.locator(".rt-section[data-section='ring-vf'] tr[data-ring='1']").first
            row.locator("input").fill("-20")
            row.locator("input").press("Enter")
            self.assertIn("1 pending V/F change", page.inner_text(".rt-pending"))
            self.edit(page, "Uncore maximum ratio", "38")
            self.assertIn("1 pending runtime change · 1 pending V/F change", page.inner_text(".rt-pending"))
            page.click("#rt-review")
            kinds = page.evaluate("window.__reviews[0].requests.map(r => typeof r === 'string' ? r : r.kind)")
            self.assertEqual(kinds, ["tune", "runtime"])
            self.assert_clean(page)

    @browser
    def test_uncore_review_apply_revert_through_the_drawer(self):
        with self.page("ring") as page:
            page.wait_for_selector("tr[data-key='max_ratio']")
            self.edit(page, "Uncore maximum ratio", "38")
            page.click("#rt-review")
            page.wait_for_selector("#apply-go")
            self.assertIn("40× → 38×", page.inner_text(".apply-section[data-kind='runtime']"))
            page.click("#apply-go")
            page.wait_for_selector(".apply-result", timeout=8000)
            page.click("#apply-close")
            page.wait_for_selector("tr[data-key='max_ratio'] .rt-now:has-text('38×')")
            page.click("#rt-revert")
            page.wait_for_selector("#apply-go")
            page.click("#apply-go")
            page.wait_for_selector(".apply-result", timeout=8000)
            page.click("#apply-close")
            page.wait_for_selector("tr[data-key='max_ratio'] .rt-now:has-text('40×')")
            self.assert_clean(page)

    # ---- Ratio caps ----
    @browser
    def test_ratio_caps_tab_lists_p_cores_and_e_clusters_with_online_state_and_no_parking(self):
        with self.page("ratios") as page:
            page.wait_for_selector(".rt-table tr[data-unit='P0']")
            self.assertEqual(page.locator(".rt-table tbody tr").count(), 12)
            self.assertEqual(page.locator("tr[data-kind='P']").count(), 8)
            self.assertEqual(page.locator("tr[data-kind='E']").count(), 4)
            self.assertIn("(cluster)", page.inner_text("tr[data-unit='EC0'] th"))
            self.assertIn("all 4 cores", page.get_attribute("tr[data-unit='EC0'] th", "title"))
            self.assertEqual(page.locator("tr[data-unit='EC0'] .rt-cpu").count(), 4)
            self.assertEqual(page.locator("tr[data-unit='EC0'] .rt-dot.off").count(), 0)
            self.assertIn("57×", page.inner_text("tr[data-unit='P0'] .rt-now"))
            self.assertIn("800–5700 MHz", page.inner_text("tr[data-unit='P0'] td:last-child"))
            self.assertIn("lost on reboot", page.inner_text(".rt-chip"))
            self.assertNotRegex(page.inner_text("#tune-panel").lower(), r"park(ing)? toggle|unpark")
            self.assertIn("Core parking is not offered", page.inner_text(".rt-info"))
            self.assertEqual(page.locator("#tune-panel input[type='checkbox']").count(), 0)
            self.assert_clean(page)

    @browser
    def test_cap_edits_are_pending_clamped_to_the_cpufreq_range_and_an_e_cluster_expands_to_its_cpus(self):
        with self.page("ratios") as page:
            page.wait_for_selector(".rt-table tr[data-unit='P0']")
            self.edit(page, "Maximum ratio of P0", "50")
            self.edit(page, "Maximum ratio of EC0", "23")
            self.assertEqual(page.inner_text(".rt-pending"), "2 pending runtime changes")
            self.edit(page, "Maximum ratio of P1", "99")  # above the cpufreq range: clamped to the CPU's 55x
            self.assertEqual(page.get_by_label("Maximum ratio of P1", exact=True).input_value(), "55")
            self.assertEqual(page.inner_text(".rt-pending"), "2 pending runtime changes", "55x is what P1 has: no edit")
            page.click("#rt-review")
            req = page.evaluate("window.__reviews[0].requests[0]")
            self.assertEqual(req["kind"], "runtime")
            self.assertEqual(req["intent"], {"caps": {"0": 50, "8": 23, "9": 23, "10": 23, "11": 23}})
            self.assert_clean(page)

    @browser
    def test_caps_review_shows_the_capped_at_note_from_the_plan_then_applies_and_reverts(self):
        with self.page("ratios") as page:
            page.wait_for_selector(".rt-table tr[data-unit='P0']")
            self.edit(page, "Maximum ratio of EC0", "23")
            self.edit(page, "Maximum ratio of P0", "50")
            page.click("#rt-review")
            page.wait_for_selector("#apply-go")
            sec = page.locator(".apply-section[data-kind='runtime']").inner_text()
            self.assertIn("cpu 8, 9, 10, 11", sec)
            self.assertIn("capped at 22", sec)
            self.assertIn("max 57× → 50×", sec)
            page.click("#apply-go")
            page.wait_for_selector(".apply-result", timeout=8000)
            page.click("#apply-close")
            page.wait_for_selector("tr[data-unit='EC0'] .rt-now:has-text('22×')")
            self.assertIn("50×", page.inner_text("tr[data-unit='P0'] .rt-now"))
            page.click("#rt-revert")
            page.wait_for_selector("#apply-go")
            page.click("#apply-go")
            page.wait_for_selector(".apply-result", timeout=8000)
            page.click("#apply-close")
            page.wait_for_selector("tr[data-unit='P0'] .rt-now:has-text('57×')")
            self.assert_clean(page)

    @browser
    def test_an_offline_cpu_is_read_only_and_caps_are_disabled_with_the_reason(self):
        with self.page("ratios", "&offline=8") as page:
            page.wait_for_selector(".rt-table tr[data-unit='EC0']")
            self.assertEqual(page.locator("tr[data-unit='EC0'] .rt-dot.off").count(), 1)
            self.assertTrue(page.get_by_label("Maximum ratio of P0", exact=True).is_disabled())
            self.assertIn("offline", page.inner_text(".rt-info"))
            self.assert_clean(page)

    @browser
    def test_unread_cpu_limits_disable_the_caps_with_read_cpu_state_first(self):
        with self.page("ratios", "&cpulimits=unread") as page:
            page.wait_for_selector(".rt-table tr[data-unit='P0']")
            self.assertTrue(page.get_by_label("Maximum ratio of P0", exact=True).is_disabled())
            self.assertIn("Read CPU state first", page.inner_text(".rt-info"))
            self.assert_clean(page)

    @browser
    def test_the_tabs_do_not_need_a_tune_read_and_the_inspector_limit_text_is_the_cpus(self):
        with self.page("cores") as page:
            self.assertNotRegex(page.inner_text(".insp-body"), r"your policy|policy\.toml")
            self.assertIn("The CPU's own offset range", page.get_attribute(".vf-table th.c-lim", "title"))
            self.assert_clean(page)

    # ---- the pending bar covers everything; the limits are the CPU's ----
    @browser
    def test_pending_bar_counts_the_runtime_edits_and_one_review_covers_vf_power_and_caps(self):
        with self.page("power") as page:
            page.wait_for_selector(".rt-table tr[data-key='pl1_w']")
            self.assertTrue(page.locator("#pending-bar").is_hidden())
            self.edit(page, "Processor Core Power Limit (PL1)", "230")
            self.assertEqual(page.inner_text("#pending-text"), "1 change on 1 unit")
            page.click("#subtab-ratios")
            page.wait_for_selector(".rt-table tr[data-unit='P0']")
            self.edit(page, "Maximum ratio of P0", "50")
            self.assertEqual(page.inner_text("#pending-text"), "2 changes on 2 units")
            page.click("#subtab-cores")
            page.click(".tile[data-unit='P0']")
            self.type_point(page, 53, "-30")  # the read holds -19.5 mV there; -20 would round to it
            self.assertEqual(page.inner_text("#pending-text"), "3 changes on 3 units")
            page.click("#pending-review")
            kinds = page.evaluate("window.__reviews[0].requests.map(r => typeof r === 'string' ? r : r.kind)")
            self.assertEqual(kinds, ["tune", "power", "runtime"])
            page.wait_for_selector("#apply-go", timeout=8000)
            self.assertEqual(page.locator(".apply-section").count(), 3)
            self.assertEqual(page.locator(".apply-section").evaluate_all("els => els.map(e => e.dataset.kind)"), ["tune", "power", "runtime"])
            page.click("#apply-go")
            page.wait_for_selector(".apply-section[data-kind='runtime'][data-status='done']", timeout=15000)
            self.assertEqual(page.locator(".apply-section[data-status='done']").count(), 3)
            self.assert_clean(page)

    @browser
    def test_pending_bar_discard_drops_the_runtime_edits_too(self):
        with self.page("power") as page:
            page.wait_for_selector(".rt-table tr[data-key='pl1_w']")
            self.edit(page, "Processor Core Power Limit (PL1)", "230")
            page.click("#pending-discard")
            self.assertEqual(page.inner_text(".rt-pending"), "No pending changes")
            self.assertTrue(page.locator("#pending-bar").is_hidden())
            self.assert_clean(page)

    @browser
    def test_drawer_amber_rows_for_a_large_cap_change_and_pl2_below_pl1(self):
        with self.page("ratios") as page:
            page.wait_for_selector(".rt-table tr[data-unit='P0']")
            self.edit(page, "Maximum ratio of P0", "20")
            page.click("#rt-review")
            page.wait_for_selector("#apply-go")
            warn = page.locator(".apply-warnings li[data-code='LARGE_CHANGE']")
            self.assertGreaterEqual(warn.count(), 1)
            self.assertIn("warn", warn.first.get_attribute("class"))
            self.assertIn("large change", warn.first.inner_text())
            self.assertFalse(page.locator("#apply-go").is_disabled(), "a warning never blocks Apply")
            self.assert_clean(page)

    @browser
    def test_unread_voltage_ceiling_negative_offsets_stay_editable_positive_and_raises_say_read_cpu_state_first(self):
        with self.page("cores", "&cpulimits=unread") as page:
            page.click(".tile[data-unit='P0']")
            self.type_point(page, 53, "-30")  # the read holds -19.5 mV there; -20 would round to it
            self.assertEqual(page.inner_text("#pending-text"), "1 change on 1 unit", "a negative offset is editable")
            page.click("#pending-discard")
            self.type_point(page, 48, "20")  # positive: clamped to 0, which is what the CPU holds at 48x: nothing pending
            self.assertTrue(page.locator("#pending-bar").is_hidden())
            text = page.inner_text(".insp-body")
            self.assertIn("Read CPU state first", text)
            self.assertNotRegex(text, r"polic")
            self.assert_clean(page)
        with self.page("turbo", "&cpulimits=unread") as page:
            expect(page.locator("#tune-panel")).to_contain_text("Read CPU state first", timeout=5000)  # the OC ratio limit is unread: no bucket may be raised
            self.assertNotRegex(page.inner_text("#tune-panel"), r"(?i)polic")
            self.assert_clean(page)

    @browser
    def test_system_screen_shows_the_cpu_limits_and_fills_power_and_caps_from_their_reads(self):
        with self.page("power") as page:
            page.evaluate("location.hash = '#/system'")
            page.wait_for_selector("#sys-limits-body tr", timeout=5000)
            text = page.inner_text("#sys-limits")
            self.assertEqual(page.locator("#sys-limits-body tr").count(), 10)
            self.assertIn("1.625 V", text)
            self.assertIn("1.999 V", text)
            self.assertIn("120×", text)
            self.assertIn("−1000…+999 mV", text)
            self.assertIn("OC mailbox 0x28", text)
            self.assertIn("writable, field maximum 4095.875 W", text, "the power read of this session fills the power row")
            self.assertIn("Read CPU state first", text, "no runtime.read yet: cpufreq / uncore are not read")
            self.assertNotRegex(page.inner_text("#sys-limits-card").lower(), r"polic")
            self.assert_clean(page)

    @browser
    def test_no_policy_word_in_the_rendered_app_text_or_tooltips(self):
        grab = "() => document.body.innerText + ' ' + [...document.querySelectorAll('[title],[aria-label],[placeholder]')].map((e) => [e.title, e.getAttribute('aria-label'), e.getAttribute('placeholder')].join(' ')).join(' ')"
        with self.page("cores") as page:
            seen = []
            page.click(".tile[data-unit='P0']")
            page.click(".tile[data-unit='EC0'] ", modifiers=["Control"])
            seen.append(page.evaluate(grab))
            for tab in ("turbo", "power", "ring", "ratios"):
                page.click(f"#subtab-{tab}")
                page.wait_for_timeout(300)
                seen.append(page.evaluate(grab))
            self.edit(page, "Maximum ratio of P0", "20")
            page.click("#rt-review")
            page.wait_for_selector("#apply-go")
            seen.append(page.evaluate(grab))
            page.click("#apply-close")
            page.evaluate("location.hash = '#/system'")
            page.wait_for_selector("#sys-limits-body tr", timeout=5000)
            seen.append(page.evaluate(grab))
            for screen in ("profiles", "audit", "dashboard", "curves", "stress"):
                page.evaluate(f"location.hash = '#/{screen}'")
                page.wait_for_timeout(500)
                seen.append(page.evaluate(grab))
            for text in seen:
                self.assertNotRegex(text, r"(?i)polic", text[:300])
            self.assert_clean(page)


if __name__ == "__main__":
    unittest.main()
