"""G-105 / G-113 web UI: the three static files are served with the CSP, and static checks hold on their source (no browser needed).

G-113 (SPEC-v3): the page now has write-capable controls, so the old "no write controls" rule became "exactly one way to write": ``tune.apply`` is
sent from one call site, inside the review dialog's confirm handler, carrying the plan id and ``CONFIRM <nonce>`` from the plan result."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from arltuner.web.server import CSP, MANIFEST
from tests.web.test_server_security import Base

WEB = Path(__file__).resolve().parents[2] / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")
CSS = (WEB / "style.css").read_text(encoding="utf-8")

# Ops the page may name at a call site: contract 8 reads, contract 9 (SPEC-v3) measure / curves / memory / tune / profiles, and cancel. Nothing else.
ALLOWED_OPS = {"preflight", "audit.tail", "telemetry.start", "cancel", "mem.read", "vf.measure", "curves.list", "curves.get", "curves.update",
               "curves.delete", "tune.read", "tune.plan", "tune.apply", "profiles.list", "profiles.save", "profiles.get", "tune.last", "stress.start", "stress.stop", "stress.list"}
# Raw contract 8 write / frequency ops stay out of the page (it uses the tune.* ops instead). G-119: the stress ops (no mailbox, no MSR write) are the
# Stress tab's.
FORBIDDEN_OPS = ("apply", "restore", "plan", "vf.dump", "telemetry.stop", "freq.cap", "freq.restore")
TABS = ("dashboard", "curves", "tune", "stress", "profiles", "system", "audit")


def func_body(name: str, async_: bool = False) -> str:
    m = re.search(r"(?:async )?function " + re.escape(name) + r"\(.*?\) \{(.*?)\n  \}\n", JS, re.S)
    assert m, name
    return m.group(1)


class TestServed(Base):
    def test_manifest_is_exactly_the_three_files(self):
        self.assertEqual(sorted(MANIFEST), ["/", "/app.js", "/style.css"])
        self.assertEqual(sorted(n for n, _ in MANIFEST.values()), ["app.js", "index.html", "style.css"])
        self.assertEqual(sorted(p.name for p in WEB.iterdir()), ["app.js", "index.html", "style.css"])

    def test_files_served_with_csp_and_content_type(self):
        ck = self.login()
        for path, name, ctype in (("/", "index.html", "text/html"), ("/app.js", "app.js", "text/javascript"), ("/style.css", "style.css", "text/css")):
            st, r, body = self.req("GET", path, cookie=ck)
            self.assertEqual(st, 200, path)
            self.assertEqual(body, (WEB / name).read_bytes(), path)
            self.assertEqual(r.getheader("Content-Security-Policy"), CSP)
            self.assertTrue(r.getheader("Content-Type").startswith(ctype), path)
            self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")

    def test_files_need_a_session(self):
        for path in ("/", "/app.js", "/style.css"):
            self.assertEqual(self.req("GET", path)[0], 401)

    def test_csp_matches_what_the_page_needs(self):
        for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "connect-src 'self'"):
            self.assertIn(directive, CSP)
        self.assertNotIn("unsafe-inline", CSP)


class TestHtml(unittest.TestCase):
    def test_no_inline_script_or_style(self):
        for m in re.finditer(r"<script\b([^>]*)>", HTML, re.I):
            self.assertRegex(m.group(1), r'\bsrc="/app\.js"')
        self.assertEqual(len(re.findall(r"<script\b", HTML, re.I)), 1)
        self.assertNotRegex(HTML, r"(?i)<style\b")
        self.assertNotRegex(HTML, r"(?i)\sstyle\s*=")
        self.assertNotRegex(HTML, r"(?i)\son[a-z]+\s*=")  # no inline event handlers
        self.assertNotRegex(HTML, r"(?i)javascript:")

    def test_no_external_resources(self):
        self.assertNotRegex(HTML, r"(?i)(https?:)?//[a-z0-9.-]+\.[a-z]{2,}")
        self.assertEqual(re.findall(r'(?i)<link\b[^>]*\bhref="([^"]*)"', HTML), ["/style.css"])
        self.assertNotRegex(HTML, r"(?i)<(img|iframe|object|embed|form|base)\b")

    def test_page_basics(self):
        self.assertIn("<title>arrowlake-tuner</title>", HTML)
        self.assertIn('name="viewport"', HTML)
        self.assertTrue(HTML.startswith("<!doctype html>"))

    def test_ids_are_unique_and_every_id_used_by_the_script_exists(self):
        ids = re.findall(r'\bid="([^"]+)"', HTML)
        self.assertEqual(len(ids), len(set(ids)))
        used = set(re.findall(r'\$\("([\w-]+)"\)', JS)) | set(re.findall(r'\b(?:setText|setPill|setTile)\("([\w-]+)"', JS))
        self.assertTrue(used)
        self.assertEqual(sorted(used - set(ids)), [])

    def test_pages_and_panels_required_by_the_task_exist(self):
        for ident in ("preflight", "banner", "helper", "core-table", "tiles", "audit", "mem-card", "refresh-range", "cv-chart", "cv-measure", "cv-lib",
                      "btn-measure", "btn-tune-read", "tn-rows", "btn-tn-plan", "btn-tn-reset", "btn-tn-restore", "tn-chart", "dlg-plan", "pr-list", "btn-pr-save"):
            self.assertIn(f'id="{ident}"', HTML)
        self.assertIn("Read ratios &amp; voltages", HTML)
        self.assertIn("each request is a WRMSR", HTML)  # stated next to the mailbox read button
        self.assertIn('id="tn-cost-n"', HTML)           # with the op count computed from the last read (D-036; no fixed estimate)
        self.assertRegex(HTML, r'<button\b[^>]*\bid="btn-tune-read"[^>]*\bdisabled\b')  # disabled until the page has proven it may run
        self.assertRegex(HTML, r'<button\b[^>]*\bid="btn-measure"[^>]*\bdisabled\b')
        self.assertRegex(HTML, r'<button\b[^>]*\bid="btn-tn-plan"[^>]*\bdisabled\b')
        self.assertIn("One curve per tuning unit", HTML)

    def test_tabs_are_an_aria_tablist(self):  # G-110, tab set from SPEC-v3
        self.assertIn('role="tablist"', HTML)
        for name in TABS:
            self.assertRegex(HTML, rf'<button\b[^>]*role="tab"[^>]*id="tab-{name}"[^>]*aria-controls="panel-{name}"')
            self.assertRegex(HTML, rf'id="panel-{name}" role="tabpanel" aria-labelledby="tab-{name}"')
        self.assertEqual(sorted(re.findall(r'role="tab"[^>]*id="(tab-[a-z]+)"', HTML)), sorted("tab-" + t for t in TABS))
        self.assertIn("ArrowRight", JS)
        self.assertIn("prefers-reduced-motion", CSS)

    def test_dialogs_are_native_and_the_html_has_only_known_controls(self):
        self.assertEqual(sorted(re.findall(r'<dialog\b[^>]*\bid="([^"]+)"', HTML)), ["dlg-confirm", "dlg-plan", "dlg-timings"])
        self.assertNotRegex(HTML, r"(?i)<textarea\b")
        for m in re.finditer(r"<input\b([^>]*)>", HTML):  # only a range (refresh), and text fields for names; never a number box for a voltage
            self.assertRegex(m.group(1), r'type="(range|text)"')
        for m in re.finditer(r"<button\b([^>]*)>", HTML):  # every button is a tab, a view control or has an id the script binds
            attrs = m.group(1)
            self.assertTrue(re.search(r'\bid="(btn|tab)-', attrs) or "data-goto=" in attrs, attrs)


class TestScript(unittest.TestCase):
    def test_no_unsafe_dom_or_code_execution(self):
        for bad in (r"innerHTML", r"outerHTML", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(", r"new\s+Function", r"setAttribute\(\s*[\"']style",
                    r"\.style\b", r"cssText", r"createContextualFragment", r"DOMParser", r"\bsessionStorage\b", r"\bindexedDB\b", r"\bdocument\.cookie\b",
                    r"\bXMLHttpRequest\b", r"\bWebSocket\b", r"setTimeout\(\s*[\"']", r"setInterval\(\s*[\"']", r"\bimport\s*\("):
            self.assertNotRegex(JS, bad)

    def test_local_storage_is_only_the_remembered_view_prefs(self):
        """SPEC-v3 section 2 wants the refresh rate remembered per browser: one key, two helpers, try/catch, nothing sensitive."""
        uses = [m.start() for m in re.finditer(r"\blocalStorage\b", JS)]
        self.assertEqual(len(uses), 2)
        self.assertIn('const PREFS_KEY = "arltuner.prefs";', JS)
        for fn in ("loadPrefs", "savePrefs"):
            self.assertRegex(func_body(fn), r"try \{.*localStorage\.(get|set)Item\(PREFS_KEY.*\} catch", fn)
        self.assertIn("JSON.stringify({ refresh_s: S.intervalS, win_min: V.winMin })", JS)  # the whole payload

    def test_no_external_urls(self):
        self.assertNotRegex(JS, r"(?i)https?://")
        self.assertNotRegex(JS, r"(?i)[\"']//[a-z]")
        self.assertEqual(sorted(set(re.findall(r"fetch\(\s*\"([^\"]+)\"", JS))), ["/api/call", "/api/status"])
        self.assertEqual(re.findall(r"new EventSource\(([^)]*)\)", JS), ["EVENTS_URL"])
        self.assertIn('const EVENTS_URL = "/api/events";', JS)  # the server's SSE route (not "/events")

    def test_post_is_json(self):
        self.assertIn('"Content-Type": "application/json"', JS)
        self.assertIn('method: "POST"', JS)
        self.assertIn('credentials: "same-origin"', JS)

    def test_only_allowed_ops_are_named(self):
        called = set(re.findall(r"\bapi\(\s*\"([a-z.]+)\"", JS))
        self.assertTrue(called <= ALLOWED_OPS, called - ALLOWED_OPS)
        self.assertEqual(called, ALLOWED_OPS)
        for op in FORBIDDEN_OPS:
            self.assertNotRegex(JS, rf"[\"']{re.escape(op)}[\"']", op)
        self.assertEqual(len(re.findall(r"\bapi\(", JS)), len(re.findall(r"\bapi\(\s*\"", JS)) + 1)  # +1: the definition; every call names its op literally

    def test_stress_tab_is_a_quick_screen_and_says_so(self):  # G-119
        self.assertIn("Quick screen — NOT an acceptance gate (D-019: Linpack 2021 60 min + SSE 60 min).", HTML)
        for ident in ("st-banner", "st-kind", "st-method", "st-dur", "st-dur-custom", "st-cap", "st-mem", "st-units", "btn-stress-start", "btn-stress-stop", "st-ring",
                      "st-errors", "st-result", "st-hist", "btn-stress-refresh"):
            self.assertIn(f'id="{ident}"', HTML)
        self.assertRegex(HTML, r'<button\b[^>]*\bid="btn-stress-start"[^>]*\bdisabled\b')
        self.assertRegex(HTML, r'<input\b[^>]*\bid="st-dur-custom"[^>]*\bmin="30"[^>]*\bmax="3600"')   # the custom length is the helper's 30..3600 s
        # SPEC 9.3: never "pass": the words are "no errors detected in N min". Checked on the page copy of the Stress tab (not on CSS class names).
        stress_js = JS[JS.index("// ---- Stress:"):JS.index("// ---- Profiles ---")]
        panel = HTML[HTML.index('id="panel-stress"'):HTML.index('id="panel-profiles"')]
        self.assertNotRegex(stress_js + panel, r"(?i)\bpass(ed|es|ing)?\b(?!\w*[-_])")
        self.assertNotRegex(stress_js, r"(?i)\bstable\b[^\n]*\bproven|\bis stable\b|\bverified stable")
        self.assertIn('"No errors detected in " + ran + "."', stress_js)
        self.assertIn("A quick screen, not an acceptance gate.", stress_js)

    def test_stress_start_sends_the_contract_arguments_and_nothing_writes(self):  # G-119
        body = func_body("startStress")
        self.assertIn('api("stress.start", args)', body)
        self.assertIn("idleGate() !== \"\"", body)          # not a mailbox gate: an idle helper with a heartbeat is enough
        for needle in ("args.method", "args.units", "args.ratio_cap", "args.memory_mb", "duration_s: secs"):
            self.assertIn(needle, body)
        self.assertEqual(len(re.findall(r"\bapi\(\s*\"stress\.start\"", JS)), 1)
        self.assertIn('api("stress.stop", {})', func_body("stopStress"))
        self.assertIn('api("stress.list", {})', func_body("loadStressList"))
        self.assertIn('st === "stressing" ? "stress"', JS)   # a heartbeat in state stressing re-attaches after a reload
        self.assertIn('"stopped_by" in r', func_body("inferKind"))

    def test_after_an_apply_the_page_offers_a_stress_screen(self):  # G-119
        self.assertIn("Validate with a stress screen", func_body("renderPlanFoot"))
        self.assertIn('selectTab("stress", true)', func_body("goValidate"))
        self.assertNotIn("startStress", func_body("goValidate"))   # it only opens the tab: nothing starts by itself

    def test_chart_minimum_spans(self):  # G-119
        self.assertIn("const MIN_SPAN = { temp: 10, power: 20, mhz: 500, volt: 0.05, gbs: 2 };", JS)
        for needle in ('minSpan: MIN_SPAN.temp', 'minSpan: MIN_SPAN.power', 'minSpan: MIN_SPAN.volt', 'minSpan: MIN_SPAN.mhz', 'minSpan: MIN_SPAN.gbs'):
            self.assertIn(needle, JS)

    def test_curves_mark_fused_points_and_stock(self):  # G-119 with G-118 shapes
        self.assertIn("p.fused_point", JS)
        self.assertIn('x === "oc"', JS)
        self.assertIn("stock = fused table as applied", JS)
        self.assertIn('id="cv-stock"', HTML)
        self.assertIn("ev.retry === true", JS)   # a retried vf.point replaces the first attempt

    def test_mailbox_read_is_never_automatic(self):
        self.assertEqual(len(re.findall(r"\bapi\(\s*\"tune\.read\"", JS)), 1)
        body = func_body("startRead")
        self.assertIn('api("tune.read"', body)
        self.assertIn('gateReason() !== ""', body)  # re-checks the gate itself
        self.assertEqual(re.findall(r"\bstartRead\b", JS).count("startRead"), 2)  # the definition and the one click listener
        self.assertIn('$("btn-tune-read").addEventListener("click", startRead);', JS)
        # the gate: preflight write, writes allowed, a heartbeat with the budget, idle helper, and the upper cost bound must fit
        gate = func_body("gateReason")
        for needle in ('S.mode !== "write"', "S.writesAllowed !== true", "heartbeatFresh()", 'S.hbState !== "idle"', "opsLeft() < readCost()"):
            self.assertIn(needle, gate)
        self.assertNotIn("COST_LOW", JS)
        self.assertNotIn("COST_HIGH", JS)

    def test_apply_is_only_sent_from_the_review_dialog_with_the_nonce(self):
        """Exactly one call site; it sits in the Apply handler; it sends the plan id and CONFIRM + the nonce of that plan itself (D-033 d: one click, no
        typing); and it is gated."""
        self.assertEqual(len(re.findall(r"\bapi\(\s*\"tune\.apply\"", JS)), 1)
        body = func_body("commitPlan")
        self.assertIn('api("tune.apply", { plan_id: P.res.plan_id, typed: "CONFIRM " + P.res.nonce })', body)
        self.assertIn('getElementById("plan-go").disabled', body)  # re-checks the dialog gate
        self.assertIn('P.state !== "review"', body)     # a plan is applied once, from its own dialog
        gate = func_body("updateModalGate")
        for needle in ("planExpired()", "applyGate(P.res.ops_needed)", 'P.state === "running"', "!changed"):
            self.assertIn(needle, gate)
        self.assertEqual(len(re.findall(r"\bcommitPlan\b", JS)), 2)  # the definition and the Apply button: no Enter-key shortcut, no typed code
        self.assertNotIn("P.typed", JS)
        self.assertNotIn("plan-typed", JS + HTML)
        self.assertNotIn('addEventListener("keydown"', func_body("renderPlanFoot"))
        self.assertIn('getElementById("plan-cancel")', func_body("openPlanDialog"))  # the dialog opens on the safe button
        # the mailbox gate for apply has the same needles as the read gate
        ag = func_body("applyGate")
        for needle in ('S.mode !== "write"', "S.writesAllowed !== true", "heartbeatFresh()", 'S.hbState !== "idle"', "opsLeft() < ops"):
            self.assertIn(needle, ag)

    def test_every_plan_goes_through_one_call_site_and_the_dialog(self):
        self.assertEqual(len(re.findall(r"\bapi\(\s*\"tune\.plan\"", JS)), 1)
        self.assertIn("openPlanDialog(body.result, intent)", func_body("requestPlan"))
        # three entry points on Tune + profiles: all via requestPlan
        self.assertIn("requestPlan({ reset: \"stock\" }", JS)
        self.assertIn("requestPlan({ restore: \"accepted\" }", JS)
        self.assertIn("requestPlan({ profile: p.name }", JS)
        self.assertIn("requestPlan(intent,", JS)

    def test_g112_g111_bindings(self):
        """The shapes the real helpers return (orchestrator note, G-111 / G-112 as merged)."""
        self.assertIn("Reset offsets to 0", HTML)                       # reset zeroes offsets only: the button says so
        self.assertNotIn("Reset to stock", HTML + JS)
        for check in ("NO_CPUFREQ", "LOAD_GENERATOR", "MEASURE_UNAVAILABLE", "JOURNAL_UNAVAILABLE", "CURVE_LIMIT", "EXISTS", "NO_SUCH_CURVE", "NO_READ",
                      "NOTHING_TO_DO", "NO_ACCEPTED_STATE", "STEP_LIMIT", "SESSION_LIMIT", "BOOT_LIMIT", "POLICY_RANGE", "TURBO_ORDER"):
            self.assertRegex(JS, rf"\b{check}: \"", check)             # every detail.check has a sentence
        self.assertIn('chk === "NO_READ"', func_body("requestPlan"))     # NO_READ sends the operator to "Read ratios & voltages"
        self.assertIn("omitted_units", JS)                               # records come in pieces; curves.get {id, units} merges them
        self.assertIn('api("curves.get", { id: rec.id, units: want })', JS)
        self.assertIn("P.partial", JS)                                   # complete:false + stop is shown as a partial apply
        self.assertIn('point === "turbo"', JS)                           # turbo read-back rows
        self.assertIn("ev.result.cpufreq", JS)                           # G-126: scaling_max_freq of the CPUs whose OC ratio changed, shown under the read-back
        self.assertIn("cpufreqNode(P.cpufreq)", func_body("planResultNode"))
        self.assertIn("detail.curve_id", JS)                             # the partial curve of a cancelled measure
        self.assertIn("body.result.note", JS)                            # profiles.list note
        self.assertIn('typeof u.id === "string"', JS)                    # tune.read names the unit
        self.assertIn("r.tune", JS)

    def test_the_intent_scope_matches_what_the_helper_accepts(self):
        """P / E targets exist only in the simple scope and unit targets only in the unit scope: mixed changes fall back to one row per unit."""
        body = func_body("buildIntent")
        self.assertIn('return { scope: "unit", rows: perUnit }', body)
        self.assertIn('{ scope: "simple", rows: merged }', body)

    def test_mailbox_actions_state_the_wrmsr_cost(self):
        self.assertIn("Each request is a WRMSR. ", JS)  # in the review dialog
        self.assertIn("each request is a WRMSR", HTML)  # next to the read button
        self.assertIn('"This plan needs "', JS)

    def test_policy_ranges_are_the_d032_numbers_and_voltages_are_dropdowns(self):
        self.assertIn("offsetMin: -200, offsetMax: 200, offsetStep: 5", JS)
        self.assertIn("vtMin: 0.900, vtMax: { P: 1.625, E: 1.355 }, vtStep: 0.005", JS)
        self.assertIn("ratioMax: { P: 57, E: 49 }", JS)
        self.assertNotRegex(JS, r'type\s*=\s*"number"')
        self.assertNotRegex(HTML, r'type="number"')
        for sel in ("offSel", "vtSel", "rSel"):  # the three row controls are <select>s
            self.assertRegex(JS, rf'const {sel} = el\("select"')
        # the only text inputs made by the script: the curve rename (the confirmation is one click now)
        self.assertEqual(sorted(re.findall(r'el\("input", "([\w-]+)"\)', JS)), ["inline-edit"])
        self.assertNotRegex(JS, r'type = "number"|type = "range"')

    def test_sse_events_handled_are_contract_events(self):
        handled = set(re.findall(r"\bon\(\"([a-z.]+)\"", JS))
        self.assertEqual(handled, {"heartbeat", "telemetry", "progress", "vf.point", "result", "state"})

    def test_unavailable_is_na_never_a_guess(self):
        self.assertIn('text: "n/a"', JS)
        self.assertNotRegex(JS, r"\?\?\s*0\b")
        self.assertNotRegex(JS, r"\|\|\s*0\b")  # no silent zero defaults for readings

    def test_uses_textcontent(self):
        self.assertIn("textContent", JS)

    def test_refresh_slider_calls_telemetry_start_with_the_chosen_step(self):
        self.assertIn("const REFRESH_STEPS = [0.25, 0.5, 1, 2, 5];", JS)
        self.assertIn('api("telemetry.start", { interval_s: S.intervalS })', JS)

    def test_curve_ids_and_profile_names_are_checked_like_the_helper(self):
        self.assertIn("const PROFILE_RE = /^[a-z0-9._-]{1,64}$/;", JS)
        self.assertIn("maxlength=\"64\"", HTML)


class TestCss(unittest.TestCase):
    def test_theme_and_layout(self):
        self.assertIn("color-scheme: dark;", CSS)  # G-110: one dark theme by design (instrument at night)
        self.assertIn("[hidden] { display: none !important; }", CSS)  # `hidden` must win over any display rule
        self.assertRegex(CSS, r"@media \(max-width: \d+px\)")
        self.assertIn("grid-template-columns", CSS)

    def test_no_external_or_script_loading(self):
        self.assertNotRegex(CSS, r"(?i)@import")
        self.assertNotRegex(CSS, r"(?i)url\(")
        self.assertNotRegex(CSS, r"(?i)https?://")
        self.assertNotRegex(CSS, r"(?i)expression\(")

    def test_status_classes_used_by_the_script_exist(self):
        for cls in ("s-pass", "s-read-only", "s-refuse", "s-unknown", "s-ok", "s-bad", "s-busy", "b-readonly", "b-refuse", "stale", "na"):
            self.assertIn(cls, CSS)

    def test_every_unit_has_a_colour_and_motion_respects_the_preference(self):
        for u in [f"P{i}" for i in range(8)] + [f"EC{i}" for i in range(4)]:
            self.assertRegex(CSS, rf"\.u-{u} \{{ --u: #[0-9a-f]{{6}}; \}}")
        for m in re.finditer(r"animation\s*:", CSS):
            before = CSS[:m.start()]
            self.assertGreater(before.rfind("prefers-reduced-motion: no-preference"), before.rfind("\n}\n"), "animation outside a no-preference block")


if __name__ == "__main__":
    unittest.main()
