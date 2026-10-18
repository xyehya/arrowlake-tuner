"""G-218 Playwright smoke for the Curves and Stress screens: loads ui/index.html?bridge=mock over a loopback HTTP server that sends the SAME
Content-Security-Policy as src-tauri/tauri.conf.json, builds a bridge around fixtures/mock-backend-g218.js (the recorded simulator answers for curves.*,
vf.measure, stress.*; the G-205 mock does not know them), mounts the view by hand on the app's store (app.js is not edited by this task) and requires
zero console errors and zero CSP violations.

No hardware, no helper. Run:  ARLTUNER_BROWSER_TESTS=1 python3 desktop/app/ui/tests/g218.smoke.py   (G218_SHOTS=<dir> also writes PNGs)
Without ARLTUNER_BROWSER_TESTS=1 the browser tests are skipped.
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

for _arg in [a for a in sys.argv if a.startswith("--shots=")]:  # --shots=<dir>: same as G218_SHOTS
    sys.argv.remove(_arg)
    os.environ["G218_SHOTS"] = _arg.split("=", 1)[1]

UI = Path(__file__).resolve().parents[1]
CONF = json.loads((UI.parent / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
CSP = CONF["app"]["security"]["csp"]
CHROME = os.environ.get("ARLTUNER_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or ""
SHOTS = os.environ.get("G218_SHOTS", "")

INIT_JS = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));
"""

# One g218 bridge per page: the recorded Curves/Stress answers wrapped around the app's own mock transport. Its events feed the app's store the way app.js
# does it for the real bridge, so the status the views gate on (helper state, telemetry) is live. The app's own bridge stops its telemetry first.
MOUNT_JS = """
async ([name, fn, opts]) => {
  const root = document.getElementById('screen');
  if (!window.__g218) {
    const { Bridge } = await import('/bridge.js');
    const { createMockTransport } = await import('/fixtures/mock-backend.js');
    const { wrapMockTransport } = await import('/fixtures/mock-backend-g218.js');
    const { applyHelperEvent } = await import('/store.js');
    const app = window.__arltuner;
    await app.bridge.call('telemetry.stop');
    const inner = createMockTransport({ motion: !!opts.motion, intervalMs: opts.tick || 50 });
    const bridge = new Bridge(wrapMockTransport(inner, { stepMs: opts.stepMs || 15, seed: opts.seed || 'none' }), 'mock');
    await bridge.connect();
    for (const ev of ['heartbeat', 'state', 'progress', 'telemetry']) bridge.on(ev, (p) => applyHelperEvent(app.store, ev, p));
    await bridge.call('telemetry.start', { interval_s: 0.05 });
    window.__g218 = { bridge, ctx: { ...app.ctx, bridge } };
  }
  const m = await import('/views/' + name + '.js');
  if (window.__view) window.__view.unmount();
  root.replaceChildren();
  window.__view = m[fn]();
  window.__view.mount(root, window.__g218.ctx);
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
    handler = functools.partial(Handler, directory=str(UI))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def browser(test):
    return unittest.skipUnless(os.environ.get("ARLTUNER_BROWSER_TESTS") == "1" and sync_playwright and CHROME, "set ARLTUNER_BROWSER_TESTS=1 (needs playwright + chrome)")(test)


class Base(unittest.TestCase):
    @contextlib.contextmanager
    def page(self, query: str, logs: list, size=(1440, 1000)):
        with serve() as origin, sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
            try:
                page = b.new_context(viewport={"width": size[0], "height": size[1]}, accept_downloads=True).new_page()
                page.add_init_script(INIT_JS)
                page.on("console", lambda m: logs.append((m.type, m.text)))
                page.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
                page.on("requestfailed", lambda r: logs.append(("requestfailed", r.url)))
                page.goto(f"{origin}/index.html{query}")
                page.wait_for_selector("html[data-ready='1']", timeout=10000)
                yield page
            finally:
                b.close()

    def mount(self, page, name: str, fn: str, **opts):
        page.evaluate(MOUNT_JS, [name, fn, opts])

    def shot(self, page, name: str):
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            page.evaluate("document.getElementById('screen').scrollTop = 0")
            page.screenshot(path=str(Path(SHOTS) / f"g218-{name}.png"))

    def assert_clean(self, page, logs):
        self.assertEqual(page.evaluate("window.__csp"), [], "CSP violations")
        bad = [m for m in logs if m[0] in ("error", "pageerror", "warning", "requestfailed") and "favicon" not in m[1] and "404" not in m[1]]
        self.assertEqual(bad, [])


class TestStress(Base):
    @browser
    def test_stress_banner_run_result_history(self):
        logs: list = []
        with self.page("?bridge=mock&motion=1&tick=50", logs) as page:
            self.mount(page, "stress", "stressView", motion=True, stepMs=40)
            banner = page.locator("#st-banner")
            expect(banner).to_be_visible()
            self.assertIn("Quick screen — NOT an acceptance gate.", banner.inner_text())
            self.assertEqual(page.locator("#st-phase").inner_text(), "not running")
            self.assertEqual(page.locator("#st-setup .unit-tile").count(), 12)
            self.assertEqual(page.locator("#st-setup .unit-tile[aria-pressed='true']").count(), 12)
            self.assertIn("Ready: integer load on all 12 units for 5 min", page.inner_text("#st-est"))
            self.assertEqual(page.locator("#st-hist").is_hidden(), True)
            expect(page.locator("#st-hist-empty")).to_contain_text("No stress screens stored yet")
            # setup: units, length, cap, method
            page.click("#st-setup .unit-quick button:has-text('E-clusters')")
            self.assertEqual(page.locator("#st-setup .unit-tile[aria-pressed='true']").count(), 4)
            page.click("#st-setup .unit-tile[data-unit='P0']")
            self.assertIn("integer load on 5 units", page.inner_text("#st-est"))
            page.click("#st-setup .seg [data-value='cache']")
            page.click("#st-setup .seg [data-value='900']")
            page.select_option("#st-cap", "48")
            self.assertIn("cache load on 5 units for 15 min, capped at 48×", page.inner_text("#st-est"))
            page.click("#st-setup .seg [data-value='custom']")
            expect(page.locator("#st-dur-custom")).to_be_visible()
            page.locator("#st-dur-custom").fill("120")
            self.assertEqual(page.inner_text("#st-dur-val"), "2 min")
            page.click("#st-setup .unit-quick button:has-text('None')")
            self.assertEqual(page.inner_text("#st-est"), "Select at least one unit.")
            self.assertTrue(page.locator("#btn-stress-start").is_disabled())
            page.click("#st-setup .unit-quick button:has-text('P-cores')")
            # memory screen: no method, no units
            page.click("#st-setup .seg [data-value='memory']")
            self.assertTrue(page.locator("#st-f-units, .st-field:has(.unit-picker)").first.is_hidden())
            expect(page.locator("#st-mem")).to_be_visible()
            page.select_option("#st-mem", "2048")
            self.assertIn("memory test of 2 GB on all CPUs", page.inner_text("#st-est"))
            page.click("#st-setup .seg [data-value='cpu']")
            # run
            page.click("#st-setup .seg [data-value='custom']")
            page.locator("#st-dur-custom").fill("30")
            page.click("#btn-stress-start")
            expect(page.locator("#st-phase")).to_have_text("running", timeout=3000)
            self.assertTrue(page.locator("#btn-stress-start").is_disabled())
            self.assertTrue(page.locator("#btn-stress-stop").is_enabled())
            self.assertTrue(page.locator("#st-setup .unit-tile").first.is_disabled())
            expect(page.locator("#st-clock")).not_to_have_text("0:00", timeout=4000)
            self.assertEqual(page.locator("#st-errors").inner_text(), "no errors so far")
            self.assertRegex(page.get_attribute("#st-ring", "stroke-dasharray"), r"^[1-9]\d*(\.\d)? 100$")
            expect(page.locator("#t-st-temp")).to_have_text(re.compile(r"^\d+ °C$"), timeout=4000)
            expect(page.locator("#t-st-pkgw")).to_have_text(re.compile(r"^\d+\.\d W$"), timeout=4000)
            expect(page.locator("#st-setup .unit-tile[data-unit='P0'] .ut-mhz")).to_have_text(re.compile(r"^\d+ MHz$"), timeout=4000)
            self.shot(page, "stress-running")
            expect(page.locator("#st-result")).to_be_visible(timeout=8000)
            self.assertEqual(page.locator("#st-phase").inner_text(), "not running")
            self.assertEqual(page.locator("#st-sum").inner_text(), "No errors detected in 30 s.")
            self.assertIn("A quick screen, not an acceptance gate.", page.inner_text("#st-result"))
            self.assertNotRegex(page.inner_text("#st-result").lower(), r"\bpass|(?<!un)stable|proven")
            self.assertEqual(page.locator("#st-result .unit-tile").count(), 2)  # the recorded P0 + EC0 run
            expect(page.locator("#st-hist-body tr")).to_have_count(1, timeout=3000)
            self.assertIn("CPU cache", page.inner_text("#st-hist-body tr"))
            self.assertEqual(page.locator("#btn-stress-start").is_enabled(), True)
            self.shot(page, "stress-result")
            self.assert_clean(page, logs)

    @browser
    def test_stress_stop_and_screen_change_keep_the_run(self):
        logs: list = []
        with self.page("?bridge=mock&motion=1&tick=50", logs) as page:
            self.mount(page, "stress", "stressView", motion=True, stepMs=60)
            page.click("#st-setup .seg [data-value='3600']")
            page.click("#btn-stress-start")
            expect(page.locator("#st-phase")).to_have_text("running", timeout=3000)
            # leave the screen and come back: the run is still shown (state lives with the bridge, not the view)
            page.evaluate("document.getElementById('screen').replaceChildren()")
            self.mount(page, "audit", "auditView")
            self.mount(page, "stress", "stressView")
            expect(page.locator("#st-phase")).to_have_text("running")
            self.assertTrue(page.locator("#btn-stress-start").is_disabled())
            page.click("#btn-stress-stop")
            expect(page.locator("#st-phase")).to_have_text("stopping", timeout=2000)
            expect(page.locator("#st-result")).to_be_visible(timeout=6000)
            self.assertRegex(page.inner_text("#st-sum"), r"^Stopped after .* of 60 min\. No errors detected in that time\.$")
            self.assertIn("stopped by you", page.inner_text("#st-result"))
            expect(page.locator("#st-hist-body tr")).to_have_count(1)
            self.assertIn("stopped", page.inner_text("#st-hist-body tr"))
            self.assert_clean(page, logs)

    @browser
    def test_stress_raw_fixture_live_readings_are_n_a_with_a_reason(self):
        logs: list = []
        with self.page("?bridge=mock&tick=50", logs) as page:
            self.mount(page, "stress", "stressView", stepMs=40, seed="recorded")
            expect(page.locator("#t-st-temp")).to_have_text("n/a")
            self.assertRegex(page.inner_text("#t-st-temp-why"), r".{4,}")  # the helper's own reason, not an empty cell
            self.assertEqual(page.locator("#t-st-pkgw").inner_text(), "n/a")
            expect(page.locator("#st-hist-body tr")).to_have_count(3, timeout=3000)  # the recorded history: two full runs and one Stop
            ended = [t.strip() for t in page.locator("#st-hist-body tr td:nth-child(5)").all_inner_texts()]
            self.assertEqual(sorted(ended), ["full length", "full length", "stopped"])
            self.assertEqual(page.locator("#st-hist-body tr td:nth-child(7)").first.inner_text().strip(), "clean")
            self.assert_clean(page, logs)


if __name__ == "__main__":
    unittest.main()
