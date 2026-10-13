"""G-129 (D-036): the accepted state is the user's. Opt-in browser tests (``ARLTUNER_BROWSER_TESTS=1``, Playwright + Chrome) against the contract-9 fake helper:

1. A fresh install has no accepted state: the page says so, Restore accepted is disabled, nothing is compared.
2. Profiles > Set as accepted: a confirm dialog naming what it replaces, then ``profiles.accept``; the compare and Restore come alive.
3. Dismissing the dialog sends nothing; a helper refusal (e.g. the profile is not applied yet) is shown in words.
No hardware, no real helper code (the helper op is tested in ``tests/helper/test_accept.py``).
"""
from __future__ import annotations

import contextlib
import os
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
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CHROME = os.environ.get("ARLTUNER_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or ""
SAVED = {"name": "mine", "source": "saved", "created": "2026-10-12T10:00:00Z",
         "intent": {"scope": "unit", "rows": [{"target": "P0", "offset": {"kind": "points", "mv": {"53": -50}}, "v_target_v": None, "ratio": None}]}}


class TestStatic(unittest.TestCase):
    def test_the_page_uses_profiles_accept_and_the_old_stub_is_gone(self):
        self.assertIn('"profiles.accept"', JS)
        self.assertNotIn("Propose as accepted", JS)
        self.assertNotIn("SPEC 6.4", JS)
        self.assertIn('id="btn-pr-accept-read"', HTML)

    def test_no_reference_machine_numbers_are_in_the_page_source(self):
        for text in (JS, HTML):
            for needle in ("p-step7", "e-p6-split3", "285K"):
                self.assertNotIn(needle, text)


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
    def session(self, *, prepare=None, chain=False, allow=()):
        from tests.web import v3_harness as h

        srv, fake, stop = h.start(seed=False)
        fake.profiles["mine"] = dict(SAVED)
        if chain:
            fake.seed_accepted_chain()
        if prepare is not None:
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
        errors = [m for m in logs if m[0] in ("error", "pageerror", "warning") and "favicon" not in m[1] and "404" not in m[1] and not any(a in m[1] for a in allow)]
        self.assertEqual(errors, [])

    def read(self, page):
        page.click("#tab-tune")
        page.wait_for_selector("#btn-tune-read:not([disabled])", timeout=10000)
        page.click("#btn-tune-read")
        page.wait_for_selector("#tn-console:not([hidden])", timeout=15000)

    # ---- 1. a fresh install ----
    def test_a_fresh_install_says_no_accepted_state_yet_and_restore_is_disabled(self):
        with self.session(prepare=lambda f: setattr(f, "no_accepted", True)) as (page, fake):
            self.read(page)
            self.assertIn("No accepted state yet", page.inner_text("#acc-summary"))
            self.assertIn("save a profile as accepted", page.inner_text("#acc-summary"))
            self.assertTrue(page.locator("#btn-tn-restore").is_disabled())
            self.assertIn("No accepted state yet", page.locator("#btn-tn-restore").get_attribute("title"))
            page.click("#tab-profiles")
            page.wait_for_selector("#pr-chain-card:not([hidden])")
            self.assertIn("No accepted state yet", page.inner_text("#pr-chain-note"))
            self.assertEqual(page.locator("#pr-chain .pr-step").count(), 0)
            self.assertEqual(page.locator("#pr-list li").count(), 1)   # only the saved profile
            self.assertEqual([r["op"] for r in fake.requests if r["op"] in ("tune.plan", "tune.apply", "profiles.accept")], [])

    # ---- G-147: a residual OC point is equivalent, not a difference ----
    def test_a_residual_oc_point_is_equivalent_and_the_comparison_says_why(self):
        with self.session(prepare=lambda f: setattr(f, "residual_equivalent", ("EC1",)), chain=True) as (page, fake):
            self.read(page)
            text = page.inner_text("#acc-summary")
            self.assertIn("Matches the accepted state on all 12 compared cores.", text)
            self.assertIn("equivalent: residual OC point (cleared by a cold boot); voltages as accepted", text)
            self.assertNotIn("Differs", text)

    # ---- 2. Set as accepted ----
    def test_set_as_accepted_confirms_then_calls_the_helper_and_restore_comes_alive(self):
        with self.session(prepare=lambda f: setattr(f, "no_accepted", True)) as (page, fake):
            self.read(page)
            page.click("#tab-profiles")
            page.wait_for_selector("#pr-list li")
            page.locator("#pr-list li", has_text="mine").locator("button.pr-accept").click()
            page.wait_for_selector("#dlg-confirm[open]")
            text = page.inner_text("#dlg-confirm")
            self.assertIn('"mine"', text)
            self.assertIn("There is no accepted state yet, so nothing is replaced", text)
            self.assertIn("Nothing is written to the CPU", text)
            self.assertEqual(fake.accept_calls, [])
            page.click("#btn-confirm-yes")
            self.until(lambda: "Accepted" in page.inner_text("#pr-status"))
            self.assertEqual(fake.accept_calls, [{"name": "mine"}])
            self.until(lambda: page.locator("#pr-chain .pr-step").count() == 1)
            page.click("#tab-tune")
            self.until(lambda: "Matches the accepted state" in page.inner_text("#acc-summary"))
            self.until(lambda: not page.locator("#btn-tn-restore").is_disabled())

    def test_the_confirm_names_what_it_replaces_and_dismissing_sends_nothing(self):
        with self.session(chain=True) as (page, fake):
            self.read(page)
            page.click("#tab-profiles")
            page.wait_for_selector("#pr-list li")
            page.locator("#pr-list li", has_text="mine").locator("button.pr-accept").click()
            page.wait_for_selector("#dlg-confirm[open]")
            text = page.inner_text("#dlg-confirm")
            for name in ("e-p6-split3", "p-boot-ramp", "p-step7"):
                self.assertIn(name, text)
            page.click("#btn-confirm-no")
            page.wait_for_selector("#dlg-confirm", state="hidden")
            self.assertEqual(fake.accept_calls, [])
            self.assertEqual(page.locator("#pr-chain .pr-step").count(), 3)
            self.assertEqual(page.locator("#pr-list li", has_text="p-step7").locator("button.pr-accept").count(), 0)   # an accepted step has no such button

    def test_the_current_state_can_be_accepted(self):
        with self.session(prepare=lambda f: setattr(f, "no_accepted", True)) as (page, fake):
            self.read(page)
            page.click("#tab-profiles")
            page.wait_for_selector("#btn-pr-accept-read:not([disabled])")
            page.click("#btn-pr-accept-read")
            page.wait_for_selector("#dlg-confirm[open]")
            self.assertIn("The state of the last read", page.inner_text("#dlg-confirm"))
            page.click("#btn-confirm-yes")
            self.until(lambda: fake.accept_calls == [{"from": "read"}])
            self.until(lambda: "Accepted" in page.inner_text("#pr-status"))

    # ---- 3. refusals ----
    def test_a_refusal_is_shown_in_words(self):
        def prepare(fake):
            fake.accept_refusal = ("NOT_APPLIED", "'mine' is not what the CPU holds now (P0 point 5: offset 0.0 mV now, -50 mV in the profile). Apply it first. Nothing was changed.")

        with self.session(prepare=prepare, allow=("403",)) as (page, fake):   # the refusal is an HTTP 403, which the browser logs
            self.read(page)
            page.click("#tab-profiles")
            page.wait_for_selector("#pr-list li")
            page.locator("#pr-list li", has_text="mine").locator("button.pr-accept").click()
            page.click("#btn-confirm-yes")
            self.until(lambda: "Not accepted" in page.inner_text("#pr-status"))
            self.assertIn("Apply it first", page.inner_text("#pr-status"))


if __name__ == "__main__":
    unittest.main()
