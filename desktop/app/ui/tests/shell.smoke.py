"""G-205 Playwright smoke for the desktop UI shell: loads ui/index.html?bridge=mock over a loopback HTTP server that sends the SAME Content-Security-Policy
as src-tauri/tauri.conf.json, goes through all seven screens (rail clicks and keys 1..7), exercises the frame (status bar, monitoring strip, pending bar,
drawer, toasts) and requires zero console errors and zero CSP violations. No hardware, no helper: the mock bridge replays fixtures.

Run:  ARLTUNER_BROWSER_TESTS=1 python3 desktop/app/ui/tests/shell.smoke.py   (or add --browser instead of the variable; needs playwright for Python + google-chrome / ARLTUNER_CHROME)
Without ARLTUNER_BROWSER_TESTS=1 the browser tests are skipped; the static tests always run. Not part of tools/run_tests.py (the file name has dots, so it
is not importable by unittest discovery); G-203 owns the hook.
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

if "--browser" in sys.argv:  # same opt-in as ARLTUNER_BROWSER_TESTS=1, for shells where setting the variable is awkward
    sys.argv.remove("--browser")
    os.environ["ARLTUNER_BROWSER_TESTS"] = "1"

UI = Path(__file__).resolve().parents[1]
CONF = json.loads((UI.parent / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
CSP = CONF["app"]["security"]["csp"]
CHROME = os.environ.get("ARLTUNER_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or ""
SCREENS = ["dashboard", "tune", "curves", "stress", "profiles", "system", "audit"]
LABELS = ["Dashboard", "Tune", "Curves", "Stress", "Profiles", "System", "Audit"]

INIT_JS = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));
"""


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args) -> None:  # quiet
        pass


@contextlib.contextmanager
def serve():
    handler = functools.partial(Handler, directory=str(UI))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


class TestStatic(unittest.TestCase):
    def test_csp_is_the_strict_tauri_one(self):
        self.assertIn("script-src 'self'", CSP)
        self.assertIn("style-src 'self'", CSP)
        self.assertNotIn("unsafe-inline", CSP)
        self.assertNotIn("unsafe-eval", CSP)

    def test_every_screen_has_a_rail_entry_and_an_icon(self):
        nav = (UI / "components" / "nav-rail.js").read_text(encoding="utf-8")
        for s, label in zip(SCREENS, LABELS):
            self.assertIn(f'id: "{s}"', nav)
            self.assertIn(f'label: "{label}"', nav)
            self.assertTrue((UI / "icons" / f"{s}.svg").is_file(), s)


def browser(test):
    return unittest.skipUnless(os.environ.get("ARLTUNER_BROWSER_TESTS") == "1" and sync_playwright and CHROME, "set ARLTUNER_BROWSER_TESTS=1 (needs playwright + chrome)")(test)


class TestShell(unittest.TestCase):
    @contextlib.contextmanager
    def page(self, query: str, logs: list):
        with serve() as origin, sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
            try:
                page = b.new_context(viewport={"width": 1280, "height": 800}).new_page()
                page.add_init_script(INIT_JS)
                page.on("console", lambda m: logs.append((m.type, m.text)))
                page.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
                page.on("requestfailed", lambda r: logs.append(("requestfailed", r.url)))
                page.goto(f"{origin}/index.html{query}")
                page.wait_for_selector("html[data-ready='1']", timeout=10000)
                yield page
            finally:
                b.close()

    def assert_clean(self, page, logs):
        self.assertEqual(page.evaluate("window.__csp"), [], "CSP violations")
        bad = [m for m in logs if m[0] in ("error", "pageerror", "warning", "requestfailed") and "favicon" not in m[1] and "404" not in m[1]]  # (Chrome asks for /favicon.ico)
        self.assertEqual(bad, [])

    @browser
    def test_all_seven_screens_by_click_and_by_key_with_no_errors_and_no_csp_violations(self):
        logs: list = []
        with self.page("?bridge=mock&motion=1&tick=100", logs) as page:
            self.assertEqual(page.locator("#nav-rail .nav-item").count(), 7)
            self.assertEqual(page.locator("#screen h1").inner_text(), "Dashboard")
            for s, label in zip(SCREENS, LABELS):
                page.click(f"#nav-{s}")
                page.wait_for_selector(f"#screen[data-screen='{s}']", state="attached")
                self.assertEqual(page.locator("#screen h1").inner_text(), label)
                self.assertEqual(page.locator("#nav-rail .nav-item.active").get_attribute("id"), f"nav-{s}")
                self.assertTrue(page.url.endswith(f"#/{s}"), page.url)
                self.assertEqual(page.title(), f"arltuner · {label}")
            for i, (s, label) in enumerate(zip(reversed(SCREENS), reversed(LABELS))):
                page.keyboard.press(str(7 - i))
                page.wait_for_selector(f"#screen[data-screen='{s}']", state="attached")
                self.assertEqual(page.locator("#screen h1").inner_text(), label)
            page.keyboard.press("8")  # no such screen: stays
            self.assertEqual(page.locator("#screen").get_attribute("data-screen"), "dashboard")
            # icons really render (mask images are same-origin and allowed by img-src)
            self.assertGreater(page.locator("#nav-tune .icon").evaluate("e => e.getBoundingClientRect().width"), 10)
            # direct deep link
            page.goto(page.url.split("#")[0] + "#/audit")
            page.wait_for_selector("#screen[data-screen='audit']", state="attached")
            self.assert_clean(page, logs)

    @browser
    def test_number_keys_are_ignored_while_typing_in_a_field(self):
        logs: list = []
        with self.page("?bridge=mock", logs) as page:
            page.evaluate("const i = document.createElement('input'); i.id = 't'; document.getElementById('screen').append(i); i.focus();")
            page.keyboard.type("3")
            self.assertEqual(page.locator("#screen").get_attribute("data-screen"), "dashboard")
            self.assertEqual(page.input_value("#t"), "3")
            self.assert_clean(page, logs)

    @browser
    def test_status_bar_shows_the_helper_mode_accepted_last_read_and_ops(self):
        logs: list = []
        with self.page("?bridge=mock&tick=100", logs) as page:
            self.assertEqual(page.inner_text("#chip-helper"), "helper connected")
            self.assertEqual(page.get_attribute("#chip-helper", "data-state"), "connected")
            self.assertEqual(page.inner_text("#chip-mode"), "read-only")
            self.assertIn("msr_allow_writes", page.get_attribute("#chip-mode", "title"))
            self.assertEqual(page.inner_text("#chip-accepted"), "accepted: unknown")
            self.assertRegex(page.inner_text("#last-read"), r"^last read \d\d:\d\d:\d\d$")
            expect(page.locator("#ops-used")).to_have_text(re.compile(r"^ops \d+ / \d+$"), timeout=5000)
            self.assertTrue(page.is_hidden("#helper-fix"))
            self.assert_clean(page, logs)

    @browser
    def test_helper_busy_and_not_installed_show_the_named_fix(self):
        for helper, state, fix in (("busy", "busy", "Close the other"), ("not_installed", "not_installed", "sudo bash install.sh"), ("not_allowed", "not_allowed", "--add-user")):
            logs: list = []
            with self.page(f"?bridge=mock&helper={helper}", logs) as page:
                self.assertEqual(page.get_attribute("#chip-helper", "data-state"), state)
                self.assertIn(fix, page.inner_text("#helper-fix"))
                self.assertEqual(page.locator("#toasts .toast.warn").count(), 1)  # stays until dismissed
                self.assertEqual(page.inner_text("#last-read"), "no read this boot")
                self.assert_clean(page, logs)

    @browser
    def test_without_a_bridge_the_page_says_so_instead_of_pretending(self):
        logs: list = []
        with self.page("", logs) as page:
            self.assertEqual(page.get_attribute("#chip-helper", "data-state"), "error")
            self.assertIn("bridge=mock", page.get_attribute("#chip-helper", "title"))
            self.assertIsNone(page.evaluate("window.__arltuner ?? null"))  # the test seam exists for the mock only
            self.assert_clean(page, logs)

    @browser
    def test_monitoring_strip_raw_fixture_is_n_a_with_reasons(self):
        logs: list = []
        with self.page("?bridge=mock&tick=50", logs) as page:
            expect(page.locator("#strip .strip-cell[data-series='pkg_power']")).to_have_attribute("title", re.compile("energy_uj missing"), timeout=5000)
            self.assertEqual(page.locator("#strip .strip-cell").count(), 6)
            self.assertEqual(page.locator("#strip .strip-cell.na").count(), 6)
            for v in page.locator("#strip .strip-value").all_inner_texts():
                self.assertEqual(v, "n/a")
            self.assertIn("energy_uj missing", page.locator("#strip .strip-cell[data-series='pkg_power']").get_attribute("title"))
            self.assertEqual(page.locator("#limit-badges .chip").all_inner_texts(), ["Power: n/a", "Current/EDP: n/a", "Thermal: n/a", "VR thermal: n/a", "Max turbo: n/a"])
            self.assert_clean(page, logs)

    @browser
    def test_monitoring_strip_live_series_collapse_and_selection(self):
        logs: list = []
        with self.page("?bridge=mock&motion=1&tick=50", logs) as page:
            expect(page.locator("#strip [data-series='pkg_power'] .strip-value")).to_have_text(re.compile(r"^\d+\.\d$"), timeout=5000)
            self.assertRegex(page.inner_text("#strip [data-series='pkg_power'] .strip-value"), r"^\d+\.\d$")
            self.assertEqual(page.inner_text("#strip [data-series='pkg_power'] .strip-unit"), "W")
            self.assertEqual(page.inner_text("#strip [data-series='dram'] .strip-value"), "n/a")  # stays unavailable even with motion
            expect(page.locator("#strip [data-series='clock'] .spark-line")).to_have_attribute("d", re.compile(r"^M.+ L.+"), timeout=5000)
            # select / deselect a series
            page.click("#strip .chip.toggle[data-series='vcore']")
            self.assertTrue(page.locator("#strip .strip-cell[data-series='vcore']").is_hidden())
            self.assertEqual(page.get_attribute("#strip .chip.toggle[data-series='vcore']", "aria-pressed"), "false")
            page.click("#strip .chip.toggle[data-series='vcore']")
            self.assertTrue(page.locator("#strip .strip-cell[data-series='vcore']").is_visible())
            # collapse / expand
            h_open = page.evaluate("document.getElementById('strip').getBoundingClientRect().height")
            page.click("#strip-toggle")
            self.assertTrue(page.locator("#strip-body").is_hidden())
            self.assertLess(page.evaluate("document.getElementById('strip').getBoundingClientRect().height"), h_open)
            self.assertEqual(page.get_attribute("#strip-toggle", "aria-expanded"), "false")
            page.click("#strip-toggle")
            self.assertTrue(page.locator("#strip-body").is_visible())
            # the strip is on every screen
            page.click("#nav-system")
            self.assertTrue(page.locator("#strip").is_visible())
            self.assert_clean(page, logs)

    @browser
    def test_pending_bar_drawer_and_toasts(self):
        logs: list = []
        with self.page("?bridge=mock", logs) as page:
            self.assertTrue(page.locator("#pending-bar").is_hidden())
            # since G-208 the count is derived from the Tune draft: make two real edits (P0 48x, EC0 15x) on the Tune screen
            page.click("#nav-tune")
            page.wait_for_selector(".tile[data-unit='P0']")
            page.evaluate(
                "Promise.all([import('/views/tune.js'), import('/lib/draft.js')]).then(([t, d]) => { const s = window.__arltuner.store; const tn = s.get().tune;"
                " t.patchTune(s, {draft: d.setPoint(tn.model, d.setPoint(tn.model, tn.draft, ['P0'], 48, -20), ['EC0'], 15, -5)}); })"
            )
            page.wait_for_selector("#pending-bar:not([hidden])")
            self.assertEqual(page.inner_text("#pending-text"), "2 changes on 2 units")
            # Review & Apply opens the drawer (non-modal: the rail still works behind it)
            page.click("#pending-review")
            page.wait_for_selector("#drawer.open")
            self.assertEqual(page.inner_text("#drawer-title"), "Review & Apply")
            page.click("#nav-tune")
            self.assertEqual(page.locator("#screen").get_attribute("data-screen"), "tune")
            self.assertTrue(page.locator("#drawer").evaluate("e => e.classList.contains('open')"))
            page.focus("#drawer-close")
            page.keyboard.press("Escape")
            page.wait_for_selector("#drawer:not(.open)", state="attached")
            self.assertTrue(page.locator("#drawer").evaluate("e => e.inert"))
            # Discard clears the draft and says so
            page.click("#pending-discard")
            self.assertTrue(page.locator("#pending-bar").is_hidden())
            self.assertIn("discarded", page.inner_text("#toasts .toast"))
            # toasts: error stays and is dismissable, ok expires
            page.evaluate("import('/store.js').then((m) => { const s = window.__arltuner.store; m.pushToast(s, {kind: 'error', text: 'boom'}); m.pushToast(s, {kind: 'ok', text: 'fine', ttlMs: 150}); })")
            page.wait_for_selector("#toasts .toast.error")
            page.wait_for_selector("#toasts .toast.ok")
            page.wait_for_selector("#toasts .toast.ok", state="detached", timeout=3000)
            self.assertEqual(page.get_attribute("#toasts .toast.error", "role"), "alert")
            page.click("#toasts .toast.error button")
            page.wait_for_selector("#toasts .toast.error", state="detached")
            self.assert_clean(page, logs)

    @browser
    def test_layout_fits_the_minimum_window(self):
        logs: list = []
        with self.page("?bridge=mock&motion=1&tick=100", logs) as page:
            page.set_viewport_size({"width": 960, "height": 600})
            page.wait_for_timeout(100)
            self.assertFalse(page.evaluate("document.documentElement.scrollWidth > window.innerWidth"))
            self.assertFalse(page.evaluate("document.body.scrollHeight > window.innerHeight"))
            self.assertTrue(page.locator("#strip").is_visible() and page.locator("#status-bar").is_visible())
            self.assert_clean(page, logs)


if __name__ == "__main__":
    unittest.main()
