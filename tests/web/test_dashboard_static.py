"""G-105 dashboard: the three static files are served with the CSP, and static checks hold on their source (no browser needed)."""
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

# Ops the page may name at a call site: read-only ones, the explicit V/F read (write-gated in the helper) and its cancel. Nothing else.
ALLOWED_OPS = {"preflight", "audit.tail", "telemetry.start", "vf.dump", "cancel"}
FORBIDDEN_OPS = ("apply", "restore", "plan", "telemetry.stop", "stress.start", "stress.stop", "freq.cap", "freq.restore")


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

    def test_panels_required_by_the_task_exist(self):
        for ident in ("preflight", "banner", "helper", "core-table", "tiles", "vf", "btn-dump", "btn-cancel", "accepted", "audit"):
            self.assertIn(f'id="{ident}"', HTML)
        self.assertIn("Read V/F curve", HTML)
        self.assertIn("112 to 124", HTML)  # the cost is stated next to the button
        self.assertRegex(HTML, r'<button\b[^>]*\bid="btn-dump"[^>]*\bdisabled\b')  # disabled until the page has proven it may run

    def test_no_write_controls(self):
        self.assertNotRegex(HTML, r"(?i)<(input|textarea|select)\b")
        buttons = re.findall(r'<button\b[^>]*\bid="([^"]+)"', HTML)
        self.assertEqual(sorted(buttons), ["btn-audit", "btn-cancel", "btn-dump", "btn-preflight"])


class TestScript(unittest.TestCase):
    def test_no_unsafe_dom_or_code_execution(self):
        for bad in (r"innerHTML", r"outerHTML", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(", r"new\s+Function", r"setAttribute\(\s*[\"']style",
                    r"\.style\b", r"cssText", r"createContextualFragment", r"DOMParser", r"\blocalStorage\b", r"\bsessionStorage\b", r"\bdocument\.cookie\b",
                    r"\bXMLHttpRequest\b", r"\bWebSocket\b", r"setTimeout\(\s*[\"']", r"setInterval\(\s*[\"']", r"\bimport\s*\("):
            self.assertNotRegex(JS, bad)

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

    def test_vf_dump_is_never_automatic(self):
        self.assertEqual(len(re.findall(r"\bapi\(\s*\"vf\.dump\"", JS)), 1)
        body = re.search(r"async function startDump\(\) \{(.*?)\n  \}\n", JS, re.S).group(1)
        self.assertIn('api("vf.dump"', body)
        self.assertIn("gateReason() !== \"\"", body)  # re-checks the gate itself
        self.assertEqual(re.findall(r"\bstartDump\b", JS).count("startDump"), 2)  # the definition and the one click listener
        self.assertIn('$("btn-dump").addEventListener("click", startDump);', JS)
        # the gate: preflight write, writes allowed, a heartbeat with the budget, idle helper, and the upper cost bound must fit
        gate = re.search(r"function gateReason\(\) \{(.*?)\n  \}\n", JS, re.S).group(1)
        for needle in ('S.mode !== "write"', "S.writesAllowed !== true", "heartbeatFresh()", 'S.hbState !== "idle"', "opsLeft() < COST_HIGH"):
            self.assertIn(needle, gate)
        self.assertIn("const COST_LOW = 112;", JS)
        self.assertIn("const COST_HIGH = 124;", JS)

    def test_sse_events_handled_are_contract_events(self):
        handled = set(re.findall(r"\bon\(\"([a-z]+)\"", JS))
        self.assertEqual(handled, {"heartbeat", "telemetry", "progress", "result", "state"})

    def test_unavailable_is_na_never_a_guess(self):
        self.assertIn('text: "n/a"', JS)
        self.assertNotRegex(JS, r"\?\?\s*0\b")
        self.assertNotRegex(JS, r"\|\|\s*0\b")  # no silent zero defaults for readings

    def test_uses_textcontent(self):
        self.assertIn("textContent", JS)


class TestCss(unittest.TestCase):
    def test_theme_and_layout(self):
        self.assertIn("prefers-color-scheme: dark", CSS)
        self.assertIn("color-scheme: light dark", CSS)
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


if __name__ == "__main__":
    unittest.main()
