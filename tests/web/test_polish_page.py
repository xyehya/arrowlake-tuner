"""Orchestrator polish 2026-10-12: static checks of the page pieces added after the reviews."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")


class TestPolish(unittest.TestCase):
    def test_fingerprint_button_calls_the_fingerprint_op_only(self):
        self.assertIn('id="btn-pr-fingerprint"', HTML)
        body = JS[JS.index("async function recordFingerprint"):JS.index("async function refreshAcceptedCompare")]
        self.assertEqual(re.findall(r'api\("([a-z.]+)"', body), ["profiles.fingerprint"])   # never profiles.accept: the accepted tuning stays

    def test_no_internal_decision_ids_in_visible_page_text(self):
        visible = re.sub(r"<!--.*?-->", "", HTML, flags=re.S)
        self.assertIsNone(re.search(r"\bD-0\d\d\b", visible))

    def test_audit_names_requests_and_filters_writes(self):
        self.assertIn('id="audit-filter"', HTML)
        self.assertIn("0x28: \"max voltage read\"", JS)
        self.assertIn("Writes & refusals", JS)

    def test_system_shows_the_limits_with_their_source(self):
        self.assertIn('id="sl-table"', HTML)
        self.assertIn("function renderSysLimits", JS)


if __name__ == "__main__":
    unittest.main()
