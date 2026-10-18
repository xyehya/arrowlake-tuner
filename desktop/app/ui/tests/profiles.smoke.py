"""G-213 Playwright smoke for the Profiles screen: list, contents, compare, Load into Tune, Review and apply (the shared drawer), Set as accepted, the fingerprint, Save, strict CSP, zero console errors.

Run:  ARLTUNER_BROWSER_TESTS=1 /usr/bin/python3 desktop/app/ui/tests/profiles.smoke.py   (or add --browser; needs playwright for Python + google-chrome / ARLTUNER_CHROME)
Without the opt-in the browser tests are skipped. Same loopback server and CSP as apply.smoke.py (the CSP is the one of src-tauri/tauri.conf.json). No hardware, no helper: the mock bridge answers
profiles.* (recorded simulator saves + an INVENTED accepted state, mock-backend.js). Knobs: `?profiles=none|accepted`, `?accept=unavailable`, `?fingerprint=incomplete`.
app.js does not route "profiles" to this view until the orchestrator wires it, so the test mounts profilesView() into #screen itself (with the app's ctx from the mock-only seam window.__arltuner).
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
# log every bridge call, then mount the Profiles view into #screen (the app still shows its placeholder for "profiles")
MOUNT_JS = """
async () => {
  const { bridge, ctx } = window.__arltuner;
  if (!window.__calls) {
    window.__calls = [];
    const call = bridge.call.bind(bridge);
    bridge.call = async (op, args) => { const res = await call(op, args); window.__calls.push({ op, args, res }); return res; };
  }
  if (window.__pv) window.__pv.unmount();
  const mod = await import('/views/profiles.js');
  const root = document.getElementById('screen');
  root.replaceChildren();
  window.__pv = mod.profilesView();
  window.__pv.mount(root, ctx);
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
    def test_profiles_files_use_no_inline_style_or_handler(self):
        for rel in ("views/profiles.js", "styles/profiles.css"):
            text = (UI / rel).read_text(encoding="utf-8")
            self.assertNotIn("innerHTML", text, rel)
            self.assertNotRegex(text, r"setAttribute\(\s*[\"']style[\"']", rel)
            self.assertNotRegex(text, r"\bon(click|input|change|keydown)\s*=", rel)
        self.assertNotIn("style=", (UI / "views/profiles.js").read_text(encoding="utf-8"))

    def test_the_view_only_calls_ops_that_write_no_hardware(self):
        text = (UI / "views/profiles.js").read_text(encoding="utf-8")
        ops = set(re.findall(r'bridge\.call\(\s*"([a-z.]+)"', text))
        self.assertEqual(ops, {"profiles.list", "profiles.get", "profiles.accept", "profiles.diff", "profiles.fingerprint", "profiles.save", "tune.last"})


class TestProfiles(unittest.TestCase):
    @contextlib.contextmanager
    def page(self, query: str = "?bridge=mock&tick=100", logs: list | None = None, width: int = 1400, height: int = 1100):
        logs = logs if logs is not None else []
        with serve() as origin, sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
            try:
                page = b.new_context(viewport={"width": width, "height": height}).new_page()
                page.add_init_script(INIT_JS)
                page.on("console", lambda m: logs.append((m.type, m.text)))
                page.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
                page.on("requestfailed", lambda r: logs.append(("requestfailed", r.url)))
                page.goto(f"{origin}/index.html{query}#/profiles")
                page.wait_for_selector("html[data-ready='1']", timeout=10000)
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
    def calls(page, op: str) -> list:
        return page.evaluate("(op) => window.__calls.filter((c) => c.op === op)", op)

    @staticmethod
    def item(page, name: str, source: str = "saved"):
        # an accepted profile that was accepted from a saved one is listed twice (same name): once as accepted, once as saved
        return page.locator(f".pr-item[data-name='{name}'][data-source='{source}']")

    @staticmethod
    def back_to_profiles(page) -> None:
        # the test mounts the view itself, so a screen change away from it has to be undone through the router first
        page.evaluate("async () => { (await import('/store.js')).setScreen(window.__arltuner.store, 'profiles'); }")
        page.evaluate(MOUNT_JS)

    @staticmethod
    def wait_list(page, n: int = 2) -> None:
        page.wait_for_function("(n) => document.querySelectorAll('#pr-list .pr-item .pr-sum').length >= n && ![...document.querySelectorAll('#pr-list .pr-sum')].some((e) => e.textContent === 'reading…')", arg=n, timeout=8000)

    @staticmethod
    def confirm(page) -> None:
        page.wait_for_selector("#pr-ask:not([hidden])", timeout=3000)
        page.click("#pr-ask-yes")

    # ---- list, contents, compare ----
    @browser
    def test_list_shows_saved_profiles_with_summary_and_an_honest_empty_accepted_state(self):
        with self.page() as page:
            self.wait_list(page)
            names = page.locator(".pr-item .pr-name").all_inner_texts()
            self.assertEqual(names, ["sim-p0-undervolt", "sim-e-light"])
            self.assertEqual(self.item(page, "sim-p0-undervolt").locator(".pr-sum").inner_text(), f"12 units · deepest {MINUS}25 mV · turbo table")
            self.assertIn("complete", self.item(page, "sim-e-light").inner_text())
            self.assertIn("2026-10-18", self.item(page, "sim-e-light").inner_text())
            self.assertEqual(page.inner_text("#pr-accepted-note"), "No accepted state yet: save a profile as accepted (Profiles).")
            self.assertEqual(page.locator("#btn-pr-restore").count(), 0)
            self.assertTrue(page.locator("#btn-pr-fingerprint").is_disabled(), "no accepted state to fingerprint")
            self.assertTrue(page.locator("#btn-pr-accept-read").is_enabled())
            self.assertEqual(page.locator(".pr-accept").count(), 2)
            self.assert_clean(page)

    @browser
    def test_show_contents_and_compare_two_profiles(self):
        with self.page() as page:
            self.wait_list(page)
            self.item(page, "sim-p0-undervolt").locator(".pr-show").click()
            row = self.item(page, "sim-p0-undervolt").locator(".pr-detail tbody tr").first
            self.assertEqual(row.locator("td").nth(0).inner_text(), "P0")
            self.assertIn(f"8 points, {MINUS}25 mV to 0 mV", row.locator("td").nth(1).inner_text())
            self.assertIn("P turbo table: 57× 57× 55×", self.item(page, "sim-p0-undervolt").inner_text())
            self.assertEqual(self.item(page, "sim-p0-undervolt").locator(".pr-show").get_attribute("aria-expanded"), "true")
            self.assertEqual(page.input_value("#pr-a"), "sim-p0-undervolt")
            self.assertEqual(page.input_value("#pr-b"), "sim-e-light")
            self.assertEqual(page.locator("#pr-diff-body tr").count(), 3)
            self.assertIn("3 differences between", page.inner_text("#pr-diff-note"))
            self.assertIn("second minus first", page.inner_text("#pr-diff-note"))
            first = page.locator("#pr-diff-body tr").first.locator("td").all_inner_texts()
            self.assertEqual(first, ["P0", "Offset at 48×", f"{MINUS}20 mV", "0 mV", "+20 mV"])
            self.assertEqual(page.inner_text("#pr-th-a"), "sim-p0-undervolt")
            page.select_option("#pr-b", "sim-p0-undervolt")
            self.assertEqual(page.inner_text("#pr-diff-note"), "Pick two different profiles.")
            self.assertTrue(page.locator("#pr-diff").is_hidden())
            self.assert_clean(page)

    @browser
    def test_empty_library_and_unavailable_accepted_state_are_said_plainly(self):
        with self.page("?bridge=mock&profiles=none") as page:
            page.wait_for_selector("#pr-empty:not([hidden])")
            self.assertIn("No profiles yet.", page.inner_text("#pr-empty"))
            self.assertEqual(page.inner_text("#pr-diff-note"), "Needs two profiles.")
            self.assert_clean(page)
        with self.page("?bridge=mock&accept=unavailable") as page:
            self.wait_list(page)
            page.click("#btn-pr-accept-read")
            self.confirm(page)
            page.wait_for_function("document.querySelector('#pr-status').textContent.startsWith('Not accepted')")
            self.assertIn("this helper keeps no accepted state", page.inner_text("#pr-status"))
            self.assertEqual(page.get_attribute("#pr-status", "data-kind"), "error")
            self.assert_clean(page)

    # ---- load into Tune ----
    @browser
    def test_load_into_tune_fills_the_shared_draft_and_asks_before_replacing_edits(self):
        with self.page() as page:
            self.wait_list(page)
            page.wait_for_function("window.__arltuner.store.get().tune.model !== null", timeout=5000)  # this boot's read, adopted by the screen itself
            self.item(page, "sim-p0-undervolt").locator(".pr-load").click()
            page.wait_for_selector(".tile[data-unit='P0']", timeout=5000)
            self.assertEqual(page.evaluate("window.__arltuner.store.get().screen"), "tune")
            self.assertEqual(page.inner_text("#pending-text"), "2 changes on 1 unit")
            self.assertEqual(page.evaluate("window.__arltuner.store.get().tune.draft.units.P0.pts"), {"48": -20, "53": -25})
            self.assertEqual(self.calls(page, "tune.apply"), [], "loading writes nothing")
            # back to Profiles: the draft is non-empty, so loading another profile asks first
            self.back_to_profiles(page)
            self.wait_list(page)
            self.item(page, "sim-e-light").locator(".pr-load").click()
            page.wait_for_selector("#pr-ask:not([hidden])")
            self.assertEqual(page.inner_text("#pr-ask-title"), "Replace the edits in Tune?")
            self.assertNotIn("null", page.inner_text("#pr-ask"))
            page.click("#pr-ask-no")
            self.assertTrue(page.locator("#pr-ask").is_hidden())
            self.assertEqual(page.evaluate("Object.keys(window.__arltuner.store.get().tune.draft.units)"), ["P0"])
            self.item(page, "sim-e-light").locator(".pr-load").click()
            self.confirm(page)
            page.wait_for_selector(".tile[data-unit='EC0']", timeout=5000)
            # the edits of the first profile are gone; the new draft is "sim-e-light minus the read". (The recorded tune.last was read after the recorded apply of P0 53x -20 mV, so the
            # whole-curve 0 mV of P0 in sim-e-light differs from the read at that one point: a fixture fact, not a loading rule.)
            self.assertEqual(page.evaluate("window.__arltuner.store.get().tune.draft.units.EC0.curveMv"), -15)
            self.assertEqual(page.evaluate("window.__arltuner.store.get().tune.draft.units.P0.curveMv"), 0)
            self.assertEqual(page.inner_text("#pending-text"), "2 changes on 2 units")
            self.assert_clean(page)

    # ---- review and apply / accept / restore / fingerprint ----
    @browser
    def test_review_apply_then_set_as_accepted_then_restore_and_fingerprint(self):
        with self.page() as page:
            self.wait_list(page)
            # a profile the CPU does not hold cannot become accepted: the helper's NOT_APPLIED refusal is shown as it is
            self.item(page, "sim-p0-undervolt").locator(".pr-accept").click()
            self.assertIn("Replace" if False else "Set as accepted?", page.inner_text("#pr-ask-title"))
            self.assertIn("There is no accepted state yet, so nothing is replaced.", page.inner_text("#pr-ask"))
            self.confirm(page)
            page.wait_for_function("document.querySelector('#pr-status').textContent.startsWith('Not accepted')")
            self.assertIn("is not what the CPU holds now", page.inner_text("#pr-status"))
            self.assertIn("Review and apply", page.inner_text("#pr-status"))
            # Review and apply: the same drawer as Tune, planned from the whole-state intent
            self.item(page, "sim-p0-undervolt").locator(".pr-review").click()
            page.wait_for_selector("#apply-go", timeout=5000)
            expect(page.locator("#drawer")).to_have_class(re.compile(r"\bopen\b"))
            self.assertEqual(self.calls(page, "tune.plan")[0]["args"], {"intent": {"profile": "sim-p0-undervolt"}})
            self.assertIn("Apply profile sim-p0-undervolt", page.inner_text("#drawer-body"))
            self.assertEqual(page.locator(".apply-steps li").count(), 3, "the conditional enable + the two points that differ from the CPU")
            self.assertEqual(self.calls(page, "tune.apply"), [], "nothing is applied until Apply")
            page.click("#apply-go")
            page.wait_for_selector(".apply-headline", timeout=8000)
            self.assertEqual(len(self.calls(page, "tune.apply")), 1)
            self.assertEqual(self.calls(page, "tune.apply")[0]["args"]["typed"].split(" ")[0], "CONFIRM")
            page.click("#drawer-close")
            # now it is what the CPU holds: accept it
            self.item(page, "sim-p0-undervolt").locator(".pr-accept").click()
            self.confirm(page)
            page.wait_for_function("document.querySelector('#pr-status').textContent.startsWith('Accepted')")
            self.assertEqual(page.inner_text("#pr-status"), 'Accepted "sim-p0-undervolt".')
            page.wait_for_selector("#pr-accepted-name")
            self.assertEqual(page.inner_text("#pr-accepted-name"), "sim-p0-undervolt")
            self.assertEqual(self.item(page, "sim-p0-undervolt", "accepted").count(), 1)
            self.assertEqual(self.item(page, "sim-p0-undervolt", "accepted").locator(".pr-accept").count(), 0, "already accepted")
            self.assertEqual(self.item(page, "sim-p0-undervolt").locator(".pr-accept").count(), 1, "the saved file of the same name stays a saved profile")
            self.assertEqual(page.inner_text("#chip-accepted"), "accepted: matches")
            # Restore accepted: the plan of {restore: "accepted"}; the CPU already holds it, so nothing changes
            page.click("#btn-pr-restore")
            page.wait_for_selector("#drawer.open .apply-lead", timeout=5000)
            self.assertEqual(self.calls(page, "tune.plan")[-1]["args"], {"intent": {"restore": "accepted"}})
            self.assertIn("Restore accepted: sim-p0-undervolt", page.inner_text("#drawer-body"))
            self.assertIn("Nothing changes", page.inner_text("#drawer-body"))
            page.click("#drawer-close")
            # the Windows fingerprint
            page.click("#btn-pr-fingerprint")
            page.wait_for_function("document.querySelector('#pr-status').textContent.startsWith('CPU fingerprint recorded')")
            self.assertIn("(complete, 24 CPUs)", page.inner_text("#pr-status"))
            self.assertIn("sim-p0-undervolt", page.inner_text("#pr-status"))
            self.assert_clean(page)

    @browser
    def test_set_the_current_state_as_accepted_replaces_with_a_confirm_that_names_what_goes(self):
        with self.page("?bridge=mock&profiles=accepted&fingerprint=incomplete") as page:
            self.wait_list(page)
            self.assertEqual(page.inner_text("#pr-accepted-name"), "read-20261018-120000")
            page.click("#btn-pr-fingerprint")
            page.wait_for_function("document.querySelector('#pr-status').textContent.startsWith('CPU fingerprint recorded')")
            self.assertIn("incomplete: no CPU identity", page.inner_text("#pr-status"))
            self.assertEqual(page.get_attribute("#pr-status", "data-kind"), "warn")
            page.click("#btn-pr-accept-read")
            page.wait_for_selector("#pr-ask:not([hidden])")
            self.assertIn("It replaces the current accepted state (read-20261018-120000).", page.inner_text("#pr-ask"))
            self.assertIn("Nothing is written to the CPU.", page.inner_text("#pr-ask"))
            page.click("#pr-ask-no")
            self.assertEqual(self.calls(page, "profiles.accept"), [], "Cancel sends nothing")
            page.click("#btn-pr-accept-read")
            self.confirm(page)
            page.wait_for_function("document.querySelector('#pr-status').textContent.startsWith('Accepted')")
            self.assertEqual(self.calls(page, "profiles.accept")[0]["args"], {"from": "read"})
            self.assertRegex(page.inner_text("#pr-status"), r'^Accepted "read-\d{8}-\d{6}", replacing read-20261018-120000\.$')
            self.assert_clean(page)

    # ---- save ----
    @browser
    def test_save_current_tune_settings_sends_rows_only_and_never_overwrites(self):
        with self.page() as page:
            self.wait_list(page)
            page.wait_for_function("window.__arltuner.store.get().tune.model !== null", timeout=5000)
            self.assertTrue(page.locator("#btn-pr-save").is_disabled())
            self.assertIn("Lowercase letters", page.inner_text("#pr-name-note"))
            page.fill("#pr-name", "Bad Name")
            self.assertIn("Not allowed", page.inner_text("#pr-name-note"))
            self.assertTrue(page.locator("#btn-pr-save").is_disabled())
            page.fill("#pr-name", "sim-e-light")
            self.assertIn("A profile with this name exists", page.inner_text("#pr-name-note"))
            self.assertTrue(page.locator("#btn-pr-save").is_disabled())
            # put a point edit into the shared draft the way Tune does, then save it
            page.evaluate("""async () => {
              const { store } = window.__arltuner;
              const draft = await import('/lib/draft.js');
              const tune = await import('/views/tune.js');
              const t = store.get().tune;
              tune.patchTune(store, { draft: draft.setPoint(t.model, t.draft, ['P0'], 48, -10) });
            }""")
            page.fill("#pr-name", "my-undervolt")
            self.assertIn("with 1 changed unit from Tune over it", page.inner_text("#pr-name-note"))
            page.click("#btn-pr-save")
            page.wait_for_function("document.querySelector('#pr-status').textContent.startsWith('Saved')")
            sent = self.calls(page, "profiles.save")[0]["args"]
            self.assertEqual(sent["name"], "my-undervolt")
            self.assertEqual(sorted(sent["intent"].keys()), ["rows", "scope"])
            self.assertEqual(sent["intent"]["rows"][0]["offset"], {"kind": "points", "mv": {"48": -10}})
            self.assertEqual(page.input_value("#pr-name"), "")
            self.assertEqual(page.locator(".pr-item .pr-name").all_inner_texts(), ["sim-p0-undervolt", "sim-e-light", "my-undervolt"])
            self.assertEqual(page.evaluate("Object.keys(window.__arltuner.store.get().tune.draft.units)"), ["P0"], "saving does not touch the draft")
            self.assert_clean(page)

    @browser
    def test_a_ring_or_turbo_edit_in_the_draft_says_apply_first_instead_of_failing(self):
        with self.page() as page:
            self.wait_list(page)
            page.wait_for_function("window.__arltuner.store.get().tune.model !== null", timeout=5000)
            page.fill("#pr-name", "with-ring")
            page.evaluate("""async () => {
              const { store } = window.__arltuner;
              const draft = await import('/lib/draft.js');
              const tune = await import('/views/tune.js');
              const t = store.get().tune;
              tune.patchTune(store, { draft: draft.setRingPoint(t.model, t.draft, 2, -10) });
            }""")
            self.assertIn("apply the ring edit first, then save", page.inner_text("#pr-name-note"))
            self.assertTrue(page.locator("#btn-pr-save").is_disabled())
            page.evaluate("""async () => {
              const { store } = window.__arltuner;
              const draft = await import('/lib/draft.js');
              const tune = await import('/views/tune.js');
              const t = store.get().tune;
              const cur = t.model.turbo.P.ratios;
              tune.patchTune(store, { draft: draft.setBucket(t.model, draft.emptyDraft(), 'P', 5, cur[5] - 1) });
            }""")
            self.assertIn("apply the turbo edit first, then save", page.inner_text("#pr-name-note"))
            self.assertTrue(page.locator("#btn-pr-save").is_disabled())
            self.assertEqual(self.calls(page, "profiles.save"), [])
            self.assert_clean(page)

    # ---- G-228: save the full state without accepting; an accept keeps history ----
    @browser
    def test_save_current_state_as_saves_the_read_without_accepting(self):
        with self.page() as page:
            self.wait_list(page)
            page.click("#btn-pr-save-state")
            page.wait_for_selector("#pr-state-name", state="visible")
            self.assertTrue(page.locator("#btn-pr-state-save").is_disabled(), "no name yet")
            page.fill("#pr-state-name", "Bad Name")
            self.assertIn("Not allowed", page.inner_text("#pr-state-note"))
            self.assertTrue(page.locator("#btn-pr-state-save").is_disabled())
            page.fill("#pr-state-name", "sim-e-light")
            self.assertIn("exists", page.inner_text("#pr-state-note"))
            page.fill("#pr-state-name", "known-good")
            page.click("#btn-pr-state-save")
            page.wait_for_function("document.querySelector('#pr-status').textContent.startsWith('Saved')")
            self.assertEqual(self.calls(page, "profiles.save")[0]["args"], {"name": "known-good", "from": "read"})
            self.assertEqual(self.calls(page, "profiles.accept"), [], "saving is not accepting")
            self.item(page, "known-good").wait_for()
            self.assertEqual(self.item(page, "known-good", "accepted").count(), 0)
            self.assertIn("No accepted state yet", page.inner_text("#pr-accepted-note"))
            self.assert_clean(page)

    @browser
    def test_save_current_state_needs_a_fresh_read_and_offers_tune(self):
        with self.page("?bridge=mock&read=stale") as page:
            self.wait_list(page)
            page.click("#btn-pr-save-state")
            page.fill("#pr-state-name", "known-good")
            page.click("#btn-pr-state-save")
            page.wait_for_selector("#pr-notice:not([hidden])")
            self.assertIn("Read CPU state first", page.inner_text("#pr-notice"))
            self.assertIn("Not saved", page.inner_text("#pr-status"))
            page.click("#btn-pr-open-tune")
            page.wait_for_function("window.__arltuner.store.get().screen === 'tune'")
            self.assert_clean(page)

    @browser
    def test_accept_confirm_lists_per_unit_differences_then_keeps_the_previous_state_with_undo(self):
        with self.page("?bridge=mock&profiles=accepted") as page:
            self.wait_list(page)
            # the CPU is changed by applying a saved profile, so the new state differs from the accepted one in P0
            self.item(page, "sim-p0-undervolt").locator(".pr-review").click()
            page.wait_for_selector("#apply-go", timeout=5000)
            page.click("#apply-go")
            page.wait_for_selector(".apply-headline", timeout=8000)
            page.click("#drawer-close")
            page.click("#btn-pr-accept-read")
            page.wait_for_selector("#pr-ask:not([hidden])")
            self.assertIn("2 differences in 1 unit", page.inner_text("#pr-ask-diff-note"))
            self.assertIn("P0 (2)", page.inner_text("#pr-ask-diff-note"))
            self.assertEqual(page.locator("#pr-ask-diff-table tbody tr").count(), 2)
            self.assertIn("saved first as a profile", page.inner_text("#pr-ask"))
            self.assertEqual(page.inner_text("#pr-ask-yes"), "Accept")
            self.assertEqual(self.calls(page, "profiles.accept"), [])
            page.click("#pr-ask-yes")
            page.wait_for_selector("#pr-notice:not([hidden])")
            result = self.calls(page, "profiles.accept")[0]["res"]["result"]
            prev = result["previous_saved_as"]
            self.assertRegex(prev, r"^accepted-before-\d{8}-\d{6}$")
            self.assertIn("Previous accepted state saved as " + prev, page.inner_text("#pr-notice"))
            # the saved list marks it and keeps it after the other saved profiles
            item = self.item(page, prev)
            item.wait_for()
            self.assertIn("previous accepted", item.inner_text())
            self.assertIn("no delete yet", page.inner_text("#pr-prev-note"))
            names = page.eval_on_selector_all("#pr-list .pr-item", "(els) => els.map((e) => e.dataset.name)")
            self.assertEqual(names[-1], prev)
            # Undo = accepting the previous state again (the mock accepts what it holds; the real helper answers NOT_APPLIED, shown with Review and apply)
            page.click("#btn-pr-undo")
            page.wait_for_function("document.querySelector('#pr-status').textContent.startsWith('Accepted')")
            self.assertEqual(self.calls(page, "profiles.accept")[1]["args"], {"name": prev})
            self.assert_clean(page)

    @browser
    def test_accept_without_a_fresh_read_says_read_cpu_state_first(self):
        with self.page("?bridge=mock&read=stale") as page:
            self.wait_list(page)
            page.click("#btn-pr-accept-read")
            self.confirm(page)
            page.wait_for_selector("#pr-notice:not([hidden])")
            self.assertIn("Read CPU state first", page.inner_text("#pr-notice"))
            self.assertIn("Not accepted", page.inner_text("#pr-status"))
            self.assertEqual(page.locator("#btn-pr-open-tune").count(), 1)
            self.assert_clean(page)

    @browser
    def test_accept_confirm_shows_a_ring_only_and_an_oc_offset_only_change(self):
        with self.page("?bridge=mock&profiles=accepted&diff=extras") as page:
            self.wait_list(page)
            page.click("#btn-pr-accept-read")
            page.wait_for_selector("#pr-ask:not([hidden])")
            self.assertIn("2 differences in 2 units", page.inner_text("#pr-ask-diff-note"))
            self.assertNotIn("No differences", page.inner_text("#pr-ask"))
            self.assertEqual(page.locator("#pr-ask-diff-table tbody tr").count(), 2)
            table = page.inner_text("#pr-ask-diff-table")
            self.assertIn("OC word: whole-curve offset", table)
            self.assertIn("ring", table)
            self.assertEqual(self.calls(page, "profiles.diff")[0]["args"], {"a": "accepted", "b": "read"})
            self.assert_clean(page)

    @browser
    def test_accept_confirm_with_no_differences_names_what_was_compared(self):
        with self.page("?bridge=mock&profiles=accepted") as page:
            self.wait_list(page)
            page.click("#btn-pr-accept-read")
            page.wait_for_selector("#pr-ask:not([hidden])")
            note = page.inner_text("#pr-ask-diff-note")
            self.assertIn("No differences in what was compared", note)
            self.assertIn("whole-curve offset", note)
            self.assertIn("Not compared: the ring", note)
            self.assert_clean(page)

    @browser
    def test_an_unreadable_accepted_state_offers_to_move_it_aside(self):
        with self.page("?bridge=mock&accept=unreadable") as page:
            self.wait_list(page)
            page.click("#btn-pr-accept-read")
            self.confirm(page)
            page.wait_for_selector("#btn-pr-aside")
            self.assertIn("cannot be read", page.inner_text("#pr-notice"))
            self.assertNotIn(".)", page.inner_text("#pr-notice"))
            self.assertEqual(self.calls(page, "profiles.accept")[0]["args"], {"from": "read"})
            page.click("#btn-pr-aside")
            page.wait_for_function("document.querySelector('#pr-status').textContent.startsWith('Accepted')")
            self.assertEqual(self.calls(page, "profiles.accept")[1]["args"], {"from": "read", "set_aside_unreadable": True})
            self.assertIn("moved, whole,", page.inner_text("#pr-status"))
            self.assert_clean(page)


if __name__ == "__main__":
    unittest.main()
