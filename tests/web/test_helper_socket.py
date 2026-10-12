"""G-144: the GUI side of the helper socket - ``connect_helper`` against a real socket service (simulator) and against hostile / broken servers. Messages are
the user-facing contract: each names the fix. No root, no systemd."""
from __future__ import annotations

import os
import socket
import stat
import threading
import time
import unittest
from pathlib import Path

from arltuner.helper.settings import Access, HelperToml
from arltuner.web.helperlink import HelperConnectError, connect_helper

from tests.helper.test_service import POSIX, ServiceCase, my_name


@unittest.skipUnless(POSIX, "Unix sockets")
class ConnectAgainstTheService(ServiceCase):
    def connect_link(self, **kw):
        link = connect_helper(str(self.path), owner_uid=os.geteuid(), **kw)
        self.addCleanup(link.close)
        return link

    def test_an_allowed_user_gets_a_working_link(self) -> None:
        self.serve()
        link = self.connect_link()
        self.assertTrue(link.alive)
        self.assertTrue(link.call("audit.tail")["ok"])

    def test_a_user_not_in_the_allowlist_is_told_how_to_be_added(self) -> None:
        self.serve(self.toml(access=Access(("somebody-else",))))
        with self.assertRaises(HelperConnectError) as ctx:
            self.connect_link(user="alice")
        self.assertEqual(ctx.exception.kind, "not_allowed")
        self.assertIn("sudo bash install.sh --add-user alice", str(ctx.exception))
        self.assertIn("allowlist", str(ctx.exception))
        for stale in ("log out", "sg arltuner", "group"):
            self.assertNotIn(stale, str(ctx.exception))

    def test_a_second_gui_is_told_another_client_is_connected(self) -> None:
        self.serve()
        first = self.connect_link()
        with self.assertRaises(HelperConnectError) as ctx:
            self.connect_link()
        self.assertEqual(ctx.exception.kind, "busy")
        self.assertIn("another client is connected", str(ctx.exception))
        self.assertTrue(first.call("audit.tail")["ok"])  # the first is unharmed

    def test_after_the_first_gui_closes_the_next_connects(self) -> None:
        svc = self.serve()
        first = self.connect_link()
        first.close()
        self.wait(lambda: svc.session is None or not svc.session.is_alive(), "session end")
        self.assertTrue(self.connect_link().call("audit.tail")["ok"])


@unittest.skipUnless(POSIX, "Unix sockets")
class ConnectErrors(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.path = self.tmp / "helper.sock"

    def connect(self, **kw):
        return connect_helper(str(self.path), owner_uid=os.geteuid(), **kw)

    def listener(self, handler, mode: int = 0o660) -> socket.socket:
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(self.path))
        os.chmod(self.path, mode)
        s.listen(4)
        self.addCleanup(s.close)

        def serve() -> None:
            try:
                conn, _ = s.accept()
            except OSError:
                return
            handler(conn)

        threading.Thread(target=serve, daemon=True).start()
        return s

    def test_missing_socket_says_run_install(self) -> None:
        with self.assertRaises(HelperConnectError) as ctx:
            self.connect()
        self.assertEqual(ctx.exception.kind, "missing")
        self.assertIn("sudo bash install.sh", str(ctx.exception))
        self.assertIn(str(self.path), str(ctx.exception))

    def test_not_a_socket_or_not_root_owned_is_never_connected_to(self) -> None:
        self.path.write_text("x")
        with self.assertRaises(HelperConnectError) as ctx:
            self.connect()
        self.assertEqual(ctx.exception.kind, "untrusted")
        self.path.unlink()
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(self.path))
        s.listen(1)
        self.addCleanup(s.close)
        with self.assertRaises(HelperConnectError) as ctx:
            connect_helper(str(self.path), owner_uid=os.geteuid() + 1)  # "owned by somebody else"
        self.assertEqual(ctx.exception.kind, "untrusted")

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_permission_denied_means_the_socket_is_broken_not_that_the_user_is_unlisted(self) -> None:
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(self.path))
        s.listen(1)
        self.addCleanup(s.close)
        os.chmod(self.path, 0)
        with self.assertRaises(HelperConnectError) as ctx:
            self.connect(user="alice")
        self.assertEqual(ctx.exception.kind, "denied")
        msg = str(ctx.exception)
        self.assertIn("file mode is wrong", msg)
        self.assertIn("0666", msg)
        self.assertIn("sudo bash install.sh", msg)
        for stale in ("log out", "sg arltuner", "--add-user", "allowlist is"):  # the allowlist is the helper's answer (not_allowed), never a connect() EACCES
            self.assertNotIn(stale, msg)

    def test_nothing_listening(self) -> None:
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(self.path))
        s.close()  # the node stays, nobody listens
        with self.assertRaises(HelperConnectError) as ctx:
            self.connect()
        self.assertEqual(ctx.exception.kind, "unavailable")
        self.assertIn("systemctl enable --now arltuner-helper.socket", str(ctx.exception))

    def test_a_helper_that_closes_at_once(self) -> None:
        self.listener(lambda conn: conn.close())
        with self.assertRaises(HelperConnectError) as ctx:
            self.connect()
        self.assertEqual(ctx.exception.kind, "closed")
        self.assertIn("journalctl -u arltuner-helper", str(ctx.exception))

    def test_a_helper_that_never_answers(self) -> None:
        held: list[socket.socket] = []
        self.listener(held.append)
        with self.assertRaises(HelperConnectError) as ctx:
            self.connect(timeout=0.5)
        self.assertEqual(ctx.exception.kind, "timeout")
        for c in held:
            c.close()

    def test_an_unknown_refusal_text_is_passed_on(self) -> None:
        self.listener(lambda conn: (conn.sendall(b'{"id":null,"ok":false,"error":{"code":"REFUSED","message":"for reasons"}}\n'), conn.close()))
        with self.assertRaises(HelperConnectError) as ctx:
            self.connect()
        self.assertIn("for reasons", str(ctx.exception))

    def test_other_null_id_errors_are_not_mistaken_for_a_refusal(self) -> None:
        # a BAD_REQUEST with a null id (an unparsable line) must not become link.refusal: the helper has not refused anybody
        from arltuner.web.helperlink import HelperLink

        import io

        class W:
            def write(self, d): return len(d)
            def flush(self): pass
            def close(self): pass

        link = HelperLink(io.BytesIO(b'{"id":null,"ok":false,"error":{"code":"BAD_REQUEST","message":"x"}}\n'), W()).start()
        link._thread.join(2)
        self.assertIsNone(link.refusal)
        link.close()


if __name__ == "__main__":
    unittest.main()
