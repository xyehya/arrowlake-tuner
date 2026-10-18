"""G-210 Playwright smoke for the apply drawer (Review & Apply -> plan -> ordered writes -> Apply -> progress -> read-back), mock bridge, strict CSP, zero console errors.

Run:  ARLTUNER_BROWSER_TESTS=1 python3 desktop/app/ui/tests/apply.smoke.py   (or add --browser; needs playwright for Python + google-chrome / ARLTUNER_CHROME)
Without the opt-in the browser tests are skipped. Same loopback server and CSP as tune.smoke.py (the CSP is the one of src-tauri/tauri.conf.json). No hardware, no helper:
the mock bridge plans from the intent and replays the apply (mock-backend.js, INVENTED parts marked there). Knobs: `?step=<ms>` slow progress, `?apply=mismatch|stale`.
The app mounts the drawer (app.js); the test uses that instance from the mock-only seam window.__arltuner.applyDrawer.
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
MOUNT_JS = """
async () => {
  const { bridge } = window.__arltuner;
  window.__calls = [];
  const call = bridge.call.bind(bridge);
  bridge.call = async (op, args) => { const res = await call(op, args); window.__calls.push({ op, args, res }); return res; };
  window.__apply = window.__arltuner.applyDrawer;
}
"""
MINUS = "−"


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
    def test_apply_files_use_no_inline_style_or_handler(self):
        for rel in ("views/tune/apply.js", "components/drawer.js", "styles/apply.css"):
            text = (UI / rel).read_text(encoding="utf-8")
            self.assertNotIn("innerHTML", text, rel)
            self.assertNotRegex(text, r"setAttribute\(\s*[\"']style[\"']", rel)
            self.assertNotRegex(text, r"\bon(click|input|change|keydown)\s*=", rel)


class TestApply(unittest.TestCase):
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
                page.evaluate(MOUNT_JS)
                self.logs = logs
                yield page
            finally:
                b.close()

    def assert_clean(self, page):
        self.assertEqual(page.evaluate("window.__csp"), [], "CSP violations")
        bad = [m for m in self.logs if m[0] in ("error", "pageerror", "warning", "requestfailed") and "favicon" not in m[1] and "404" not in m[1]]
        self.assertEqual(bad, [])

    @staticmethod
    def edit(page, ratio: int, value: str, unit: str = "P0") -> None:
        page.click(f".tile[data-unit='{unit}']")
        field = page.locator(f".insp-section tr[data-ratio='{ratio}'] input").first
        field.click()
        page.keyboard.type(value)
        page.keyboard.press("Enter")

    @staticmethod
    def calls(page, op: str) -> list:
        return page.evaluate("(op) => window.__calls.filter((c) => c.op === op)", op)

    def review(self, page) -> None:
        self.edit(page, 48, "-20")
        self.assertEqual(page.inner_text("#pending-text"), "1 change on 1 unit")
        page.click("#pending-review")

    # ---- review ----
    @browser
    def test_review_shows_ordered_writes_warnings_and_unit_before_after(self):
        with self.page() as page:
            self.review(page)
            page.wait_for_selector("#apply-go", timeout=5000)
            expect(page.locator("#drawer")).to_have_class(re.compile(r"\bopen\b"))
            self.assertEqual(page.inner_text("#drawer-title"), "Review & Apply")
            self.assertEqual(self.calls(page, "tune.plan")[0]["args"]["intent"]["rows"][0]["offset"], {"kind": "points", "mv": {"48": -20}})
            steps = page.locator(".apply-steps li")
            self.assertEqual(steps.count(), 2)
            self.assertIn("only if not already on", steps.nth(0).inner_text())
            self.assertIn(f"P0 point 4 offset {MINUS}20 mV", steps.nth(1).inner_text())
            self.assertGreaterEqual(page.locator(".apply-warnings li").count(), 2)
            self.assertEqual(page.locator(".apply-warnings li[data-code='NO_BASELINE']").count(), 1)
            row = page.locator(".apply-units tr[data-unit='P0']")
            self.assertIn("pt 4 · 48×", row.inner_text())
            self.assertIn(f"{MINUS}19.5 mV", row.inner_text())
            self.assertEqual(row.locator(".apply-pill").inner_text(), "queued")
            self.assertIn("Predicted VccIA", page.inner_text(".apply-vccia"))
            self.assertIn("Revert: cold boot", page.inner_text(".apply-revert"))
            self.assertEqual(page.inner_text("#apply-go"), "Apply 2 writes")
            self.assertEqual(page.evaluate("document.activeElement.id"), "apply-close", "focus goes to the safe button")
            self.assertEqual(self.calls(page, "tune.apply"), [], "reviewing sends nothing")
            self.assert_clean(page)

    # ---- apply, progress, result ----
    @browser
    def test_apply_sends_confirm_nonce_shows_progress_and_a_read_back_result(self):
        with self.page("?bridge=mock&tick=100&step=70") as page:
            self.review(page)
            page.wait_for_selector("#apply-go")
            nonce = self.calls(page, "tune.plan")[0]["res"]["result"]["nonce"]
            page.click("#apply-go")
            page.wait_for_selector("#apply-cancel", timeout=3000)
            page.wait_for_selector(".apply-units tr[data-status='writing']", timeout=3000)
            self.assertEqual(page.inner_text("#drawer-title"), "Applying…")
            self.assertTrue(page.is_visible(".apply-bar"))
            self.assertRegex(page.inner_text(".apply-progress"), r"^step \d+ of \d+ · [\d.]+ (s|ms) elapsed")
            # the window stays usable: another tile can be selected while the apply runs
            page.click(".tile[data-unit='P1']")
            self.assertEqual(page.inner_text(".insp-title"), "P-core 1")
            self.assertTrue(page.is_visible("#drawer.open"))
            page.wait_for_selector(".apply-result", timeout=8000)
            apply_call = self.calls(page, "tune.apply")[0]
            self.assertEqual(apply_call["args"]["typed"], "CONFIRM " + nonce)
            self.assertRegex(nonce, r"^[A-Z2-9]{6}$")
            self.assertEqual(apply_call["args"]["plan_id"], self.calls(page, "tune.plan")[0]["res"]["result"]["plan_id"])
            self.assertEqual(page.inner_text(".apply-headline"), "1/1 words read back as written")
            self.assertEqual(page.locator(".apply-units tr[data-unit='P0'] .apply-pill").inner_text(), "read back OK")
            self.assertEqual(page.inner_text("#drawer-title"), "Applied")
            self.assertEqual(page.locator(".apply-readback tbody tr").count(), 1)
            self.assertEqual(page.locator(".apply-readback tbody tr").get_attribute("data-match"), "true")
            # success: the draft is cleared and tune.last was re-read
            self.assertTrue(page.is_hidden("#pending-bar"))
            self.assertEqual(page.evaluate("Object.keys(window.__arltuner.store.get().tune.draft.units).length"), 0)
            self.assertGreaterEqual(len(self.calls(page, "tune.last")), 1)
            self.assertEqual(page.inner_text("#apply-close"), "Close")
            self.assert_clean(page)

    @browser
    def test_two_units_are_listed_in_write_order_and_end_read_back_ok(self):
        with self.page("?bridge=mock&tick=100&step=40") as page:
            self.edit(page, 48, "-20", "P0")
            self.edit(page, 48, "-15", "P1")
            page.click("#pending-review")
            page.wait_for_selector("#apply-go")
            units = page.locator(".apply-units tbody tr").evaluate_all("rows => rows.map(r => r.dataset.unit)")
            self.assertEqual(units[0], "P0")
            self.assertGreaterEqual(len(units), 2)
            page.click("#apply-go")
            page.wait_for_selector(".apply-result", timeout=10000)
            seen = page.evaluate("window.__arltuner.store.get().apply.sections[0].progress")
            self.assertEqual(seen["step"], seen["of"])
            for u in units:
                self.assertEqual(page.locator(f".apply-units tr[data-unit='{u}'] .apply-pill").inner_text(), "read back OK")

    # ---- cancel ----
    @browser
    def test_cancel_sends_cancel_with_the_op_id_and_shows_the_stop(self):
        with self.page("?bridge=mock&tick=100&step=80") as page:
            self.review(page)
            page.wait_for_selector("#apply-go")
            page.click("#apply-go")
            page.wait_for_selector("#apply-cancel")
            self.assertIn("between transactions", page.inner_text(".apply-cancel-note"))
            page.click("#apply-cancel")
            page.wait_for_selector(".apply-section[data-status='cancelled']", timeout=5000)
            cancel = self.calls(page, "cancel")
            self.assertEqual(len(cancel), 1)
            apply_id = page.evaluate("window.__arltuner.store.get().apply.sections[0].opId")
            self.assertEqual(cancel[0]["args"], {"op_id": apply_id})
            self.assertIn("Cancelled between transactions", page.inner_text(".apply-error-box"))
            self.assertEqual(page.inner_text("#drawer-title"), "Cancelled")
            # the draft is kept (nothing was confirmed) and the app is usable again
            self.assertTrue(page.is_visible("#pending-bar"))
            self.assertTrue(page.is_enabled("#pending-review"))
            self.assert_clean(page)

    # ---- mismatch ----
    @browser
    def test_mismatch_result_is_shown_and_the_draft_is_kept(self):
        with self.page("?bridge=mock&tick=100&apply=mismatch") as page:
            self.review(page)
            page.wait_for_selector("#apply-go")
            page.click("#apply-go")
            page.wait_for_selector(".apply-result", timeout=8000)
            self.assertEqual(page.inner_text(".apply-headline"), "1 of 1 words differ on read-back")
            self.assertEqual(page.locator(".apply-units tr[data-unit='P0'] .apply-pill").inner_text(), "mismatch")
            self.assertTrue(page.locator(".apply-readback").evaluate("e => e.open"), "the read-back table opens on a mismatch")
            self.assertEqual(page.locator(".apply-readback tr[data-match='false']").count(), 1)
            self.assertIn("Do not trust this state", page.inner_text(".apply-result"))
            self.assertEqual(page.inner_text("#drawer-title"), "Applied, with problems")
            self.assertTrue(page.is_visible("#pending-bar"), "a state that did not read back keeps the draft")
            self.assert_clean(page)

    # ---- plan error ----
    @browser
    def test_plan_error_lands_on_the_offending_field(self):
        with self.page() as page:
            self.edit(page, 48, "-180")  # inside the editor's own -200 clamp, outside the mock policy (-150)
            page.click("#pending-review")
            page.wait_for_selector("#apply-plan-error", timeout=5000)
            self.assertIn("P0: offset -180 mV", page.inner_text("#apply-plan-error"))
            self.assertEqual(page.locator("#apply-go").count(), 0, "there is nothing to apply")
            field = page.locator(".insp-section tr[data-ratio='48'] input").first
            self.assertEqual(field.get_attribute("aria-invalid"), "true")
            self.assertIn("outside the policy window", page.inner_text(".insp-section tr[data-ratio='48'] .field-error-note"))
            self.assertEqual(page.evaluate("document.activeElement === document.querySelector(\".insp-section tr[data-ratio='48'] input\")"), True)
            self.assertTrue(page.locator(".tile[data-unit='P0']").evaluate("e => e.classList.contains('plan-error')"))
            # the mark survives a re-render of the inspector ...
            page.click(".tile[data-unit='P1']")
            page.click(".tile[data-unit='P0']")
            self.assertEqual(page.locator(".insp-section tr[data-ratio='48'] input").first.get_attribute("aria-invalid"), "true")
            # ... and goes away with the next edit
            field.click()
            page.keyboard.type("-100")
            page.keyboard.press("Enter")
            expect(page.locator(".field-error-note")).to_have_count(0)
            self.assertIsNone(page.locator(".insp-section tr[data-ratio='48'] input").first.get_attribute("aria-invalid"))
            self.assertEqual(self.calls(page, "tune.apply"), [])
            self.assert_clean(page)

    # ---- PLAN_STALE ----
    @browser
    def test_plan_stale_offers_replan_and_the_new_plan_applies(self):
        with self.page("?bridge=mock&tick=100&apply=stale") as page:
            self.review(page)
            page.wait_for_selector("#apply-go")
            first = self.calls(page, "tune.plan")[0]["res"]["result"]["plan_id"]
            page.click("#apply-go")
            page.wait_for_selector("#apply-replan", timeout=5000)
            self.assertEqual(page.inner_text("#drawer-title"), "Plan is stale")
            self.assertIn("Nothing was written", page.inner_text(".apply-error-box"))
            self.assertEqual(page.locator("#apply-go").count(), 0)
            self.assertEqual(len(self.calls(page, "tune.plan")), 1, "no automatic re-plan")
            page.click("#apply-replan")
            page.wait_for_selector("#apply-go", timeout=5000)
            plans = self.calls(page, "tune.plan")
            self.assertEqual(len(plans), 2)
            self.assertEqual(plans[1]["args"], plans[0]["args"], "the same request is planned again")
            self.assertNotEqual(plans[1]["res"]["result"]["plan_id"], first)
            page.click("#apply-go")
            page.wait_for_selector(".apply-result", timeout=8000)
            self.assertEqual(page.inner_text(".apply-headline"), "1/1 words read back as written")
            self.assert_clean(page)

    # ---- non-modal ----
    @browser
    def test_the_drawer_is_non_modal_and_reopens_while_an_apply_runs(self):
        with self.page("?bridge=mock&tick=100&step=90") as page:
            self.review(page)
            page.wait_for_selector("#apply-go")
            page.click("#apply-go")
            page.wait_for_selector("#apply-cancel")
            page.keyboard.press("Escape")  # closes the drawer, not the op
            expect(page.locator("#drawer")).not_to_have_class(re.compile(r"\bopen\b"))
            page.click("#nav-dashboard")
            page.wait_for_selector("#screen[data-screen='dashboard']", state="attached")
            page.click("#nav-tune")
            page.wait_for_selector("#screen[data-screen='tune']", state="attached")
            page.click("#pending-review")  # reopens; no second plan, no second apply
            page.wait_for_selector("#apply-cancel")
            expect(page.locator("#drawer")).to_have_class(re.compile(r"\bopen\b"))
            self.assertEqual(len(self.calls(page, "tune.plan")), 1)
            self.assertEqual(len(self.calls(page, "tune.apply")), 1)
            page.wait_for_selector(".apply-result", timeout=8000)
            self.assert_clean(page)

    @browser
    def test_finishing_while_the_drawer_is_closed_toasts_the_summary(self):
        with self.page("?bridge=mock&tick=100&step=60") as page:
            self.review(page)
            page.wait_for_selector("#apply-go")
            page.click("#apply-go")
            page.wait_for_selector("#apply-cancel")
            page.keyboard.press("Escape")
            page.wait_for_selector("#toasts .toast.ok", timeout=8000)
            self.assertIn("words read back as written", page.inner_text("#toasts .toast.ok"))
            self.assert_clean(page)


if __name__ == "__main__":
    unittest.main()
