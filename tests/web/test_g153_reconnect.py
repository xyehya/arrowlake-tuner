"""G-153: after the helper service restarted, the GUI server reconnects on the next call (once per call, the same peer checks as the first connect)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arltuner.web.helperlink import HelperConnectError, connect_helper
from arltuner.web.server import Config, WebServer
from tests.web.fake_helper import FakeHelper
from tests.web.test_server_security import WEB_ROOT, Base


class Reconnect(Base):
    def setUp(self) -> None:
        super().setUp()
        self.attempts = 0
        self.next_fake: FakeHelper | None = None
        self.srv.reconnect = self.reconnect

    def reconnect(self):
        self.attempts += 1
        if self.next_fake is None:
            raise HelperConnectError("missing", "the helper is not started")
        return self.next_fake.link

    def test_the_next_call_after_a_restart_reconnects_once_and_succeeds(self) -> None:
        ck = self.login()
        self.assertEqual(self.call("preflight", cookie=ck)[0], 200)
        self.fake.close()  # the service restarted: the link is dead
        self.next_fake = FakeHelper()
        self.addCleanup(self.next_fake.close)
        self.assertEqual(self.call("preflight", cookie=ck)[0], 200)
        self.assertEqual(self.attempts, 1)
        self.assertEqual(self.call("preflight", cookie=ck)[0], 200)
        self.assertEqual(self.attempts, 1)  # a live link is not reconnected
        st, _, body = self.req("GET", "/api/status", cookie=ck)
        self.assertTrue(json.loads(body)["helper_alive"])

    def test_a_failed_reconnect_is_a_503_and_the_next_call_tries_once_more(self) -> None:
        ck = self.login()
        self.fake.close()
        self.assertEqual(self.call("preflight", cookie=ck)[0], 503)
        self.assertEqual(self.attempts, 1)  # once per call, no loop
        self.assertEqual(self.call("preflight", cookie=ck)[0], 503)
        self.assertEqual(self.attempts, 2)
        self.next_fake = FakeHelper()
        self.addCleanup(self.next_fake.close)
        self.assertEqual(self.call("preflight", cookie=ck)[0], 200)

    def test_the_events_stream_attaches_to_the_new_link(self) -> None:
        ck = self.login()
        self.fake.close()
        self.next_fake = FakeHelper()
        self.addCleanup(self.next_fake.close)
        self.assertEqual(self.call("preflight", cookie=ck)[0], 200)
        self.assertIs(self.srv.helper, self.next_fake.link)
        self.assertTrue(self.srv.helper.alive)

    def test_without_a_reconnect_function_nothing_changes(self) -> None:
        self.srv.reconnect = None
        ck = self.login()
        self.fake.close()
        self.assertEqual(self.call("preflight", cookie=ck)[0], 503)

    def test_the_peer_checks_still_apply_to_the_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            node = Path(d) / "helper.sock"
            node.write_text("not a socket")
            self.srv.reconnect = lambda: connect_helper(str(node))  # untrusted: not a socket owned by root
            ck = self.login()
            self.fake.close()
            self.assertEqual(self.call("preflight", cookie=ck)[0], 503)
            self.assertFalse(self.srv.helper.alive)


if __name__ == "__main__":
    unittest.main()
