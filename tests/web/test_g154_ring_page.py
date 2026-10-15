"""G-154 (D-045): the pages must not break on the ring (cache, P1 = 2) -- ``tune.read`` returns a ``ring`` unit, ``accepted_match`` has a ``cpu: "ring"`` entry, a restore plan
lists ``ring`` in its units / before / after / steps and its apply reports ring read-back rows. No new controls: the existing renderers show what they show generically.

Static checks always; the browser flow needs ``ARLTUNER_BROWSER_TESTS=1`` (Playwright + Chrome) against the contract-9 fake helper (no real helper code, no hardware)."""
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
CHROME = os.environ.get("ARLTUNER_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or ""

RING_POINTS = [{"point": n, "word": f"0x{w:08x}", "ratio": r, "v_target_raw": 0, "v_target_v": 0.0, "static": False, "offset_raw": o, "offset_mv": round(o * 1000 / 1024, 3)}
               for n, (r, w, o) in enumerate([(8, 8, 0), (14, 14, 0), (20, 20, 0), (28, 28, 0), (34, 34, 0), (37, 37, 0), (38, 38, 0), (39, 0xFCC00027, -26), (40, 0xFCC00028, -26)], start=1)]
RING = {"unit": "ring", "cpu": 0, "oc": {"word": "0x00000028", "ratio": 40, "v_target_raw": 0, "v_target_v": 0.0, "static": False, "offset_raw": 0, "offset_mv": 0.0},
        "points": RING_POINTS, "end": "rc 0x11 at P2=10"}


def with_ring(fake, *, ring_matches: bool) -> None:
    """Wrap the fake's replies the way the real helper answers once the ring is part of the state."""
    original = fake._reply

    def inject_read(res: dict) -> None:
        res["ring"] = RING
        res["ops_ring"] = 11
        if isinstance(res.get("accepted_match"), list):
            res["accepted_match"] = [*res["accepted_match"], {"cpu": "ring", "unit": "ring", "match": ring_matches, "diffs": [] if ring_matches else
                                                              [{"point": 8, "expected": "0xfcc00027", "actual": "0x00000027"}]}]

    def reply(rid, ok, **kw):
        res = kw.get("result")
        if ok and isinstance(res, dict):
            if "units" in res and "accepted_match" in res and "plan_id" not in res:
                inject_read(res)
            elif isinstance(res.get("result"), dict) and "accepted_match" in res["result"]:  # tune.last
                inject_read(res["result"])
            if "plan_id" in res and "steps" in res and "before" in res:  # a plan: the ring joins the units, before/after and steps
                res["units"] = [*res["units"], "ring"]
                res["before"]["ring"] = {"8": {"ratio": 39, "offset_mv": 0.0, "v_target_v": None}}
                res["after"]["ring"] = {"8": {"ratio": 39, "offset_mv": -25.391, "v_target_v": None}}
                res["steps"] = [*res["steps"], {"kind": "write_ring_point", "unit": "ring", "cpu": 0, "point": 8, "offset_mv": -25}]
                res["writes"] = res.get("writes", 0) + 1
                res["warnings"] = [*res.get("warnings", []), {"code": "RING_NOT_RESTORED", "text": "the ring (cache) will NOT be written by this plan: ring_oc_differs (planner): test"}]
            if "readback" in res and "plan_id" in res:  # an apply
                res["readback"] = [*res["readback"], {"unit": "ring", "point": 8, "cpu": 0, "expected": "0xfcc00027", "actual": "0xfcc00027", "match": True}]
                res["ring_refused"] = []
        return original(rid, ok, **kw)

    fake._reply = reply


class TestStatic(unittest.TestCase):
    def test_the_page_has_no_ring_control_but_the_plan_diff_knows_the_ring_unit(self) -> None:
        self.assertNotIn("btn-ring", JS)
        self.assertIn('const RING_UNIT = "ring";', JS)
        self.assertIn("UNIT_IDS.indexOf(u) < 0 && u !== RING_UNIT", JS)  # a unit the page does not know is skipped by the diff, the ring is one it lists
        self.assertIn("UNIT_IDS.concat([RING_UNIT])", JS)  # ... so a restore that only needs the ring still has changes (the Apply gate says "Nothing would change" otherwise)


@unittest.skipUnless(os.environ.get("ARLTUNER_BROWSER_TESTS") == "1" and sync_playwright and CHROME, "set ARLTUNER_BROWSER_TESTS=1 (needs playwright + chrome)")
class TestBrowser(unittest.TestCase):
    @staticmethod
    def until(cond, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.1)
        raise AssertionError("condition not reached")

    @contextlib.contextmanager
    def session(self, *, ring_matches: bool):
        from tests.web import v3_harness as h

        srv, fake, stop = h.start(seed=False)
        fake.seed_accepted_reference()
        with_ring(fake, ring_matches=ring_matches)
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

    def read(self, page) -> None:
        page.click("#tab-tune")
        page.wait_for_selector("#btn-tune-read:not([disabled])", timeout=10000)
        page.click("#btn-tune-read")
        page.wait_for_selector("#tn-console:not([hidden])", timeout=15000)

    def test_a_ring_that_differs_is_one_more_compared_unit_and_the_page_stays_whole(self) -> None:
        with self.session(ring_matches=False) as (page, fake):
            self.read(page)
            text = page.inner_text("#acc-summary")
            self.assertIn("Differs from the accepted state on 1 of 13 cores", text)
            self.assertIn("cpu ring", text)  # the generic renderer names the entry by its cpu field: "ring"
            self.assertEqual(page.locator("#tn-rows .tn-rowwrap").count() > 0, True)

    def test_a_matching_ring_counts_as_one_more_matching_unit(self) -> None:
        with self.session(ring_matches=True) as (page, fake):
            self.read(page)
            self.assertIn("Matches the accepted state on all 13 compared cores.", page.inner_text("#acc-summary"))

    def test_a_restore_plan_with_ring_steps_opens_applies_and_reads_back_without_errors(self) -> None:
        with self.session(ring_matches=True) as (page, fake):
            self.read(page)
            self.until(lambda: not page.locator("#btn-tn-restore").is_disabled())
            page.click("#btn-tn-restore")
            page.wait_for_selector("#dlg-plan[open]")
            body = page.inner_text("#dlg-plan")
            self.assertIn("write steps", body)
            self.assertIn("ring_oc_differs", body)  # the plan warning of a refused ring is shown as the generic warning list shows any
            self.until(lambda: not page.locator("#plan-go").is_disabled())
            page.click("#plan-go")
            self.until(lambda: "Read-back" in page.inner_text("#dlg-plan"))
            result = page.inner_text("#dlg-plan")
            self.assertIn("ring", result)
            self.assertIn("pt 8", result)


if __name__ == "__main__":
    unittest.main()
