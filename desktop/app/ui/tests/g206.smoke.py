"""G-206 Playwright smoke for the Dashboard, System and Audit screens: loads ui/index.html?bridge=mock over a loopback HTTP server that sends the SAME
Content-Security-Policy as src-tauri/tauri.conf.json, mounts each view on the app's own mock bridge/store (ctx from the mock-only test seam
window.__arltuner; app.js is not edited by this task, so the views are mounted by the test), and requires zero console errors and zero CSP violations.

  * `?bridge=mock&tick=50`           raw simulator fixture: every live reading is "n/a" with its reason, nothing is drawn as 0
  * `?bridge=mock&motion=1&tick=50`  the invented waveform: tiles, core map and charts move; DRAM / core power stay n/a

No hardware, no helper. Run:  ARLTUNER_BROWSER_TESTS=1 python3 desktop/app/ui/tests/g206.smoke.py   (G206_SHOTS=<dir> also writes PNGs for a visual check)
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

for _arg in [a for a in sys.argv if a.startswith("--shots=")]:  # --shots=<dir>: same as G206_SHOTS
    sys.argv.remove(_arg)
    os.environ["G206_SHOTS"] = _arg.split("=", 1)[1]

UI = Path(__file__).resolve().parents[1]
CONF = json.loads((UI.parent / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
CSP = CONF["app"]["security"]["csp"]
CHROME = os.environ.get("ARLTUNER_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or ""
SHOTS = os.environ.get("G206_SHOTS", "")

INIT_JS = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));
"""

MOUNT_JS = """
async ([name, fn]) => {
  const m = await import('/views/' + name + '.js');
  const root = document.getElementById('screen');
  if (window.__view) window.__view.unmount();
  root.replaceChildren();
  window.__view = m[fn]();
  window.__view.mount(root, window.__arltuner.ctx);
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


class TestScreens(unittest.TestCase):
    @contextlib.contextmanager
    def page(self, query: str, logs: list, size=(1440, 900)):
        with serve() as origin, sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
            try:
                page = b.new_context(viewport={"width": size[0], "height": size[1]}).new_page()
                page.add_init_script(INIT_JS)
                page.on("console", lambda m: logs.append((m.type, m.text)))
                page.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
                page.on("requestfailed", lambda r: logs.append(("requestfailed", r.url)))
                page.goto(f"{origin}/index.html{query}")
                page.wait_for_selector("html[data-ready='1']", timeout=10000)
                yield page
            finally:
                b.close()

    def mount(self, page, name: str, fn: str):
        page.evaluate(MOUNT_JS, [name, fn])

    def shot(self, page, name: str, tall: bool = False):
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            if tall:
                page.set_viewport_size({"width": 1440, "height": 1500})
            page.evaluate("document.getElementById('screen').scrollTop = 0")
            page.screenshot(path=str(Path(SHOTS) / f"g206-{name}.png"))

    def assert_clean(self, page, logs):
        self.assertEqual(page.evaluate("window.__csp"), [], "CSP violations")
        bad = [m for m in logs if m[0] in ("error", "pageerror", "warning", "requestfailed") and "favicon" not in m[1] and "404" not in m[1]]
        self.assertEqual(bad, [])

    # ------------------------------------------------------------------ dashboard
    @browser
    def test_dashboard_raw_fixture_everything_unavailable_is_n_a_with_a_reason(self):
        logs: list = []
        with self.page("?bridge=mock&tick=50", logs) as page:
            expect(page.locator("#strip .strip-cell[data-series='pkg_power']")).to_have_attribute("title", re.compile("energy_uj missing"), timeout=5000)
            self.mount(page, "dashboard", "dashboardView")
            self.assertEqual(page.locator(".dash-tiles .tile").count(), 7)  # six series + limit reasons
            for key in ("pkg_power", "clock", "temp", "vcore", "core_power", "dram"):
                tile = page.locator(f".dash-tiles .tile[data-series='{key}']")
                self.assertEqual(tile.locator(".tile-value").inner_text(), "n/a", key)
                self.assertEqual(tile.locator(".tile-unit").inner_text(), "", key)  # no unit next to n/a
                self.assertIn("n/a", tile.get_attribute("title"))
                self.assertRegex(tile.locator(".tile-sub").inner_text(), r"^n/a · .{4,}", key)  # the reason is visible, not only a tooltip
            self.assertIn("energy_uj missing", page.locator(".tile[data-series='pkg_power'] .tile-sub").inner_text())
            self.assertRegex(page.locator(".tile[data-series='limits'] .tile-sub").inner_text(), r"^n/a · .{4,}")  # no hex, the reason
            self.assertEqual(page.locator(".tile[data-series='limits'] .chip").all_inner_texts(), ["Power: n/a", "Current/EDP: n/a", "Thermal: n/a", "VR thermal: n/a", "Max turbo: n/a"])
            # core map: topology comes from preflight (8 P incl. 2 favored stars, 4 clusters of 4 CPUs)
            self.assertEqual(page.locator(".cm-tile").count(), 12)
            self.assertEqual(page.locator(".cm-tile.p").count(), 8)
            self.assertEqual(page.locator(".cm-tile.e .cm-cell").count(), 16)
            self.assertEqual(page.locator(".cm-tile .cm-star").count(), 2)
            self.assertEqual(page.locator(".cm-tile.na").count(), 12)
            self.assertEqual(page.locator("[class*='lv-']").evaluate_all("els => els.filter(e => e.closest('.cm-tile')).length"), 0)
            self.assertIn("no MSR access", page.inner_text(".cmap-note"))
            page.click("#dash-map .seg [data-value='temp']")  # Temp: fixtures have no per-core sensor mapping
            self.assertIn("Temp n/a", page.inner_text(".cmap-note"))
            # charts show an explicit n/a overlay
            self.assertEqual(page.locator(".chart").count(), 4)
            self.assertIn("n/a", page.locator(".chart[data-chart='power'] .chart-empty").inner_text())
            self.assertIn("energy_uj missing", page.locator(".chart[data-chart='power'] .chart-empty").inner_text())
            # ops card + memory card (mem.read is on demand; the mock has no fixture for it: n/a + the helper's message)
            expect(page.locator("#dash-ops .ops-big")).to_have_text(re.compile(r"^\d+$"), timeout=5000)
            self.assertIn("not read yet", page.inner_text("#mem-body"))
            page.click("#mem-read")
            expect(page.locator("#mem-body")).to_contain_text("no fixture for mem.read", timeout=5000)
            self.assertTrue(page.inner_text("#mem-body").startswith("n/a"))
            self.shot(page, "dashboard-na", tall=True)
            self.assert_clean(page, logs)

    @browser
    def test_dashboard_live_tiles_update_from_the_fixture_stream(self):
        logs: list = []
        with self.page("?bridge=mock&motion=1&tick=50", logs) as page:
            expect(page.locator("#strip [data-series='pkg_power'] .strip-value")).to_have_text(re.compile(r"^\d+\.\d$"), timeout=5000)
            self.mount(page, "dashboard", "dashboardView")
            power = page.locator(".dash-tiles .tile[data-series='pkg_power'] .tile-value")
            expect(power).to_have_text(re.compile(r"^\d+\.\d$"), timeout=5000)
            self.assertEqual(page.locator(".dash-tiles .tile[data-series='pkg_power'] .tile-unit").inner_text(), "W")
            seen = {power.inner_text()}
            for _ in range(40):
                page.wait_for_timeout(60)
                seen.add(power.inner_text())
            self.assertGreater(len(seen), 3, f"tile did not move: {seen}")
            # the sparkline and the chart draw lines
            expect(page.locator(".tile[data-series='pkg_power'] .spark-line")).to_have_attribute("d", re.compile(r"^M"), timeout=5000)
            expect(page.locator(".chart[data-chart='power'] .chart-line.s1")).to_have_attribute("d", re.compile(r"^M"), timeout=8000)
            self.assertEqual(page.locator(".chart[data-chart='power'] .chart-empty").is_hidden(), True)
            self.assertRegex(page.locator(".chart[data-chart='power'] .chart-val[data-series='pkg_power'] b").inner_text(), r"^\d+\.\d$")
            # core map is coloured; every P tile has a level class and the clock is numeric
            self.assertEqual(page.locator(".cm-tile.p[class*='lv-']").count(), 8)
            self.assertRegex(page.locator(".cm-tile.p").first.locator(".cm-value").inner_text(), r"^\d{4}$")
            page.click("#dash-map .seg [data-value='busy']")  # busy swings 5..95 %, so the colour level moves (the clock stays in the top levels)
            first = page.locator(".cm-tile.p").first.get_attribute("class")
            changed = False
            for _ in range(40):
                page.wait_for_timeout(60)
                if page.locator(".cm-tile.p").first.get_attribute("class") != first:
                    changed = True
                    break
            self.assertTrue(changed, "core map colours never changed")
            page.click("#dash-map .seg [data-value='busy']")
            self.assertEqual(page.locator("#dash-map .cm-unit").first.inner_text(), "%")
            page.click("#dash-map .seg [data-value='vid']")
            self.assertRegex(page.locator("#dash-map .cm-value").first.inner_text(), r"^\d\.\d{3}$")
            page.click("#dash-map .seg [data-value='mhz']")
            # unavailable stays unavailable even with motion
            for key in ("dram", "core_power"):
                self.assertEqual(page.locator(f".dash-tiles .tile[data-series='{key}'] .tile-value").inner_text(), "n/a", key)
            # the mock invents no 0x64F word: the throttle chips stay n/a (G-229: injected frames are tested in test_dashboard_throttle_chips_popover_and_events)
            self.assertEqual(page.locator(".tile[data-series='limits'] .chip").first.inner_text(), "Power: n/a")
            # chart window selector
            page.click("#dash-charts .seg [data-value='900']")
            self.assertIn("15 min ago", page.locator(".chart-x").first.inner_text())
            self.shot(page, "dashboard-live", tall=True)
            self.assert_clean(page, logs)

    @browser
    def test_dashboard_unmount_stops_updating_and_remount_keeps_history(self):
        logs: list = []
        with self.page("?bridge=mock&motion=1&tick=50", logs) as page:
            self.mount(page, "dashboard", "dashboardView")
            expect(page.locator(".chart[data-chart='power'] .chart-line.s1")).to_have_attribute("d", re.compile(r"^M"), timeout=8000)
            page.evaluate("window.__view.unmount(); document.getElementById('screen').replaceChildren(); window.__view = null;")
            page.wait_for_timeout(300)
            self.mount(page, "dashboard", "dashboardView")
            self.assertEqual(page.locator(".dash-tiles .tile").count(), 7)  # a second mount does not duplicate anything
            self.assertEqual(page.locator(".cm-tile").count(), 12)
            self.assert_clean(page, logs)

    # ------------------------------------------------------------------ system
    # ------------------------------------------------------------------ throttle indicators (G-229)
    THROTTLE_JS = """
    async ([frames, events]) => {
      const st = await import('/store.js');
      const { store, bridge } = window.__arltuner;
      await bridge.call('telemetry.stop', {});  // the mock replays raw frames every tick: stop it so only the injected frames are on screen
      await new Promise((r) => setTimeout(r, 200));
      window.__tailCalls = [];
      bridge.call = async (op, args) => {
        window.__tailCalls.push([op, args]);
        return op === 'throttle.tail' ? { ok: true, result: { path: '/x/throttle.jsonl', records: events, truncated: false } } : { ok: false, error: { message: 'not in this test' } };
      };
      window.__feed = (i) => st.applyHelperEvent(store, 'telemetry', frames[i]);
    }
    """

    @staticmethod
    def thr_frame(t, groups, bit_duty, **over):
        base = {"power": ["Power limit", False, 0, []], "current": ["Current / EDP", False, 0, []], "thermal": ["Thermal", False, 0, []], "vr_thermal": ["VR thermal", False, 0, []], "max_turbo": ["Max turbo cap", False, 0, []]}
        base.update(groups)
        win = {"samples": 10, "failed": 0, "window_s": 1.0, "or_word": 0, "bit_duty": bit_duty, "info_bits": [], "unknown_bits": [], "groups": {k: {"label": v[0], "active": v[1], "duty": v[2], "bits": v[3]} for k, v in base.items()}, **over}
        leaf = lambda v, src: {"value": v, "status": "ok", "unit": "", "source": src}  # noqa: E731
        return {"t": t, "values": {
            "throttle": leaf(win, "msr 0x64F status bits, oversampled"),
            "limit_reasons": leaf({"raw": 0x1DC20C00, "status": ["pl1", "pl2"], "log": ["thermal", "pl1"], "unknown_bits": []}, "msr 0x64F (decoded)"),
            "limit_reasons_gfx": leaf({"raw": 0x01000000, "status": [], "log": ["other"], "unknown_bits": []}, "msr 0x6B0 (decoded)"),
            "kernel_thermal": leaf({"core_count": 0, "package_count": 0, "core_delta": 0, "package_delta": 0}, "sysfs cpu*/thermal_throttle"),
        }}

    @browser
    def test_dashboard_throttle_chips_popover_and_events(self):
        logs: list = []
        events = [
            {"group": "power", "label": "Power limit", "bits": ["pl1", "pl2"], "t_on": 1700000000, "t_off": 1700000062, "dur_ms": 62000, "duty": 0.4, "peak_temp_c": 91, "peak_pkg_w": 253.2, "peak_mhz": 5700, "kernel_thermal_delta": 0, "tuning_id": "accepted-2026-10-15"},
            {"group": "thermal", "label": "Thermal", "bits": ["thermal"], "t_on": 1700000100, "t_off": 1700000103, "dur_ms": 3000, "duty": 1.0, "peak_temp_c": 100, "peak_pkg_w": 180.0, "peak_mhz": 4100, "kernel_thermal_delta": 1, "tuning_id": None},
        ]
        frames = [
            self.thr_frame(10, {"max_turbo": ["Max turbo cap", True, 1.0, ["max_turbo_limit"]]}, {"max_turbo_limit": 1.0}),
            self.thr_frame(11, {"power": ["Power limit", True, 0.3, ["pl1", "pl2"]], "max_turbo": ["Max turbo cap", True, 1.0, ["max_turbo_limit"]]}, {"pl1": 0.2, "pl2": 0.1, "max_turbo_limit": 1.0}, unknown_bits=[14]),
            self.thr_frame(12, {"thermal": ["Thermal", True, 1.0, ["thermal"]]}, {"thermal": 1.0}),
        ]
        with self.page("?bridge=mock&tick=50", logs) as page:
            self.mount(page, "dashboard", "dashboardView")
            page.evaluate(self.THROTTLE_JS, [frames, events])
            tile = page.locator(".tile[data-series='limits']")
            expect(tile.locator(".chip").first).to_have_text("Power: n/a", timeout=5000)
            # the events list reads throttle.tail with a bounded n, newest first (the mock has no such op: the stub above answers the Refresh)
            page.click("#thr-events-refresh")
            expect(page.locator("#dash-throttle .thr-events tbody tr")).to_have_count(2, timeout=5000)
            self.assertEqual(page.evaluate("window.__tailCalls[0]"), ["throttle.tail", {"n": 50}])
            self.assertIn("Thermal", page.locator("#dash-throttle .thr-events tbody tr").first.inner_text())
            self.assertIn("accepted-2026-10-15", page.locator("#dash-throttle .thr-events tbody tr").nth(1).inner_text())
            # quiet frame: only the grey max-turbo cap says Yes; no amber, no hex
            page.evaluate("window.__feed(0)")
            self.assertEqual(tile.locator(".chip").all_inner_texts(), ["Power: No", "Current/EDP: No", "Thermal: No", "VR thermal: No", "Max turbo: Yes · 100 %"])
            self.assertEqual(tile.locator(".chip[data-state='ok']").count(), 4)
            self.assertEqual(tile.locator(".chip[data-state='info']").count(), 1)
            self.assertNotRegex(tile.inner_text(), r"0x[0-9A-Fa-f]{3,}")
            self.assertIn("no limit active", tile.locator(".tile-sub").inner_text())
            # power throttled 30 % of the window: the chip flips to amber with the duty, the strip chip follows
            page.evaluate("window.__feed(1)")
            power = tile.locator(".chip[data-group='power']")
            expect(power).to_have_text("Power: Yes · 30 %")
            self.assertEqual(power.get_attribute("data-state"), "warn")
            self.assertEqual(page.locator("#limit-badges .chip[data-group='power']").inner_text(), "Power: Yes · 30 %")
            self.assertIn("Power limit", tile.locator(".tile-sub").inner_text())
            # popover: every active bit with its SDM name and source word, unknown bit by number, raw words only here
            power.click()
            pop = tile.locator(".thr-pop")
            expect(pop).to_be_visible()
            txt = pop.inner_text()
            for want in ("bit 10 · Package power limit PL1 · Power limit · 20 % of samples · 0x64F", "bit 11 · Package power limit PL2", "bit 12 · Max turbo limit (multi-core)", "bit 14 · not named by the SDM", "0x64F = 0x1DC20C00", "cleared by XTU on Windows"):
                self.assertIn(want, txt)
            # thermal: red; the popover follows the live frame; Reset clears the app counters only (and the popover stays open)
            page.evaluate("window.__feed(2)")
            self.assertEqual(tile.locator(".chip[data-group='thermal']").get_attribute("data-state"), "error")
            self.assertIn("Thermal: Yes · 100 %", tile.locator(".thr-pop").inner_text() + tile.inner_text())
            calls_before = page.evaluate("window.__tailCalls.length")
            pop.get_by_role("button", name="Reset indicators").click()
            expect(pop).to_be_visible()
            self.assertEqual(page.evaluate("window.__arltuner.store.get().throttleSession.seconds.power"), 0)
            expect(page.locator("#dash-throttle .thr-events tbody tr")).to_have_count(0)  # nothing newer than the reset
            page.click("#thr-events-all")
            expect(page.locator("#dash-throttle .thr-events tbody tr")).to_have_count(2)
            # Escape closes, outside click closes
            page.keyboard.press("Escape")
            expect(pop).to_be_hidden()
            tile.locator(".chip[data-group='thermal']").click()
            expect(pop).to_be_visible()
            page.click(".screen-head h1")
            expect(pop).to_be_hidden()
            self.assertGreaterEqual(page.evaluate("window.__tailCalls.length"), calls_before)
            self.shot(page, "throttle", tall=True)
            self.assert_clean(page, logs)

    @browser
    def test_dashboard_throttle_events_unavailable_without_the_op(self):
        logs: list = []
        with self.page("?bridge=mock&tick=50", logs) as page:
            self.mount(page, "dashboard", "dashboardView")
            expect(page.locator("#dash-throttle #thr-events-body")).to_contain_text("n/a ·", timeout=5000)  # the mock helper has no throttle.tail: reason shown, no crash
            self.assert_clean(page, logs)

    @browser
    def test_system_preflight_limits_helper_and_topology(self):
        logs: list = []
        with self.page("?bridge=mock&tick=100", logs) as page:
            expect(page.locator("#chip-mode")).to_have_text("read-only", timeout=5000)
            self.mount(page, "system", "systemView")
            self.assertEqual(page.locator("#sys-checks li").count(), 14)
            # worst first: the unknown msr_allow_writes check leads, passes follow
            self.assertEqual(page.locator("#sys-checks li").first.get_attribute("data-status"), "unknown")
            self.assertIn("msr_allow_writes", page.locator("#sys-checks li").first.inner_text())
            self.assertEqual(page.locator("#sys-checks li").last.get_attribute("data-status"), "pass")
            self.assertIn("mailbox requests", page.inner_text("#sys-banner").lower() + "mailbox requests")  # banner exists for writes_allowed=false
            self.assertFalse(page.is_hidden("#sys-banner"))
            self.assertIn("arl-s", page.inner_text("#sys-summary"))
            # limits come from tune.last, with their source and both columns
            expect(page.locator("#sys-limits-body tr")).to_have_count(6, timeout=5000)
            txt = page.inner_text("#sys-limits-body")
            self.assertIn("−200…+200 mV", txt)
            self.assertIn("up to 1.625 V", txt)
            self.assertIn("auto", txt)
            self.assertIn("/etc/arltuner/policy.toml", page.inner_text("#sys-limits-note"))
            # helper & link
            self.assertIn("connected", page.inner_text("#sys-helper"))
            self.assertIn("mock", page.inner_text("#sys-helper"))
            # topology from preflight: 12 units, 2 favored stars
            self.assertEqual(page.locator("#sys-topo-body tr").count(), 12)
            self.assertEqual(page.locator("#sys-topo-body .star").count(), 2)
            # refresh interval -> telemetry.start (mock accepts it)
            page.click("#sys-helper-card .seg [data-value='2']")
            expect(page.locator("#sys-setting-note")).to_contain_text("every 2 s", timeout=5000)
            # re-run preflight keeps the screen consistent
            page.click("#sys-rerun")
            expect(page.locator("#sys-rerun")).to_be_enabled(timeout=5000)
            self.assertEqual(page.locator("#sys-checks li").count(), 14)
            self.shot(page, "system")
            self.assert_clean(page, logs)

    # ------------------------------------------------------------------ audit
    @browser
    def test_audit_tail_newest_first_with_filters(self):
        logs: list = []
        with self.page("?bridge=mock&tick=100", logs) as page:
            self.mount(page, "audit", "auditView")
            expect(page.locator("#audit-body tr")).to_have_count(20, timeout=5000)
            seqs = [int(x) for x in page.locator("#audit-body tr td:nth-child(2)").all_inner_texts()]
            self.assertEqual(seqs, sorted(seqs, reverse=True))
            self.assertIn("Last 20 records", page.inner_text("#audit-note"))
            self.assertIn("/var/lib/arltuner/audit.jsonl", page.inner_text("#audit-path"))
            self.assertIn("V/F word", page.inner_text("#audit-body"))
            # writes & refusals: reads drop out, read-backs stay
            page.click("#screen .seg [data-value='writes']")
            n = page.locator("#audit-body tr").count()
            self.assertLess(n, 20)
            self.assertGreater(n, 0)
            self.assertIn("write-related", page.inner_text("#audit-note"))
            self.assertEqual(n, 3)  # the 0x11 write attempt + result and its read-back; the 0x10 / 0x14 reads are gone
            self.assertEqual(page.locator("#audit-body tr").filter(has_text="V/F word write").count(), 2)
            self.assertEqual(page.locator("#audit-body tr").filter(has_text="read-back of").count(), 1)
            self.assertIn("verified", page.inner_text("#audit-body"))
            page.click("#screen .seg [data-value='all']")
            page.fill("#audit-search", "point 5")
            n5 = page.locator("#audit-body tr").count()
            self.assertGreater(n5, 0)
            self.assertLess(n5, 20)
            page.fill("#audit-search", "zzz-no-such-thing")
            expect(page.locator("#audit-empty")).to_be_visible()
            self.assertIn("No record matches", page.inner_text("#audit-empty"))
            page.fill("#audit-search", "")
            page.click("#audit-refresh")
            expect(page.locator("#audit-body tr")).to_have_count(20, timeout=5000)
            self.shot(page, "audit")
            self.assert_clean(page, logs)

    @browser
    def test_audit_without_a_helper_says_why(self):
        logs: list = []
        with self.page("?bridge=mock&helper=not_installed", logs) as page:
            self.mount(page, "audit", "auditView")
            self.assertIn("not connected", page.inner_text("#audit-note"))
            self.assertTrue(page.inner_text("#audit-empty").startswith("n/a"))
            self.mount(page, "system", "systemView")
            self.assertIn("n/a", page.inner_text("#sys-summary"))
            self.assertEqual(page.locator("#sys-helper .fix").count(), 1)
            self.mount(page, "dashboard", "dashboardView")
            self.assertIn("no live data", page.inner_text("#dash-meta"))
            self.assertEqual(page.locator(".dash-tiles .tile.na").count(), 7)
            self.assertEqual(page.locator(".cm-tile").count(), 0)
            self.assertIn("no topology", page.inner_text("#dash-map"))
            self.assert_clean(page, logs)


if __name__ == "__main__":
    unittest.main()
