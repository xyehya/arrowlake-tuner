"""G-144: the launcher as a real process in socket mode (``tools/arltuner-helper.py --sim --socket``): its own socket, a systemd-style ``LISTEN_FDS`` handover,
real SIGTERM, idle exit, usage errors. Simulator and temp paths only; no root, no systemd."""
from __future__ import annotations

import os
import signal
import socket
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

from .test_process import LAUNCHER, POSIX, ProcessCase
from .test_service import Client, my_group, my_name

HANDOVER = (
    "import os, sys\n"
    "fd = int(sys.argv[1])\n"
    "if fd != 3:\n    os.dup2(fd, 3)\n"
    "os.set_inheritable(3, True)\n"
    "os.environ['LISTEN_PID'] = str(os.getpid())\n"
    "os.environ['LISTEN_FDS'] = '1'\n"
    "os.execv(sys.executable, [sys.executable, '-I', *sys.argv[2:]])\n"
)


@unittest.skipUnless(POSIX, "Unix sockets, signals")
class SocketProcessTests(ProcessCase):
    def setUp(self) -> None:
        super().setUp()
        self.run_dir = self.tmp / "run"
        self.sock = self.run_dir / "helper.sock"
        self.toml = self.tmp / "helper.toml"
        self.write_toml(f'[access]\nusers = ["{my_name()}"]\ngroup = "{my_group()}"\n')

    def write_toml(self, text: str) -> None:
        self.toml.write_text(text, encoding="utf-8")
        os.chmod(self.toml, 0o600)

    def args(self, *extra: str) -> list[str]:
        return ["--sim", "--socket", "--socket-path", str(self.sock), "--helper-toml", str(self.toml), "--audit", str(self.tmp / "audit.jsonl"),
                "--lock", str(self.tmp / "lock"), *extra]

    def start(self, *extra: str) -> subprocess.Popen[bytes]:
        p = subprocess.Popen([sys.executable, "-I", str(LAUNCHER), *self.args(*extra)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmp)
        self.procs.append(p)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if os.path.lexists(self.sock) and stat.S_IMODE(os.lstat(self.sock).st_mode) == 0o660:
                time.sleep(0.05)
                return p
            if p.poll() is not None:
                raise AssertionError(f"exited early: {p.stderr.read()!r}")
            time.sleep(0.02)
        raise AssertionError("no socket")

    def test_serves_a_client_and_stops_on_sigterm_removing_its_socket(self) -> None:
        p = self.start()
        c = Client(self.sock)
        self.assertTrue(c.call("1", "preflight")["ok"])
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(10), 0)
        self.assertFalse(os.path.lexists(self.sock))
        c.close()
        err = p.stderr.read()
        self.assertNotIn(b"Traceback", err)
        self.assertIn(b"client connected", err)

    def handed_over(self, mode: int = 0o660) -> subprocess.Popen[bytes]:
        """A helper started the way the socket unit starts it: the listening socket arrives as fd 3."""
        self.run_dir.mkdir(mode=0o755, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(self.sock))
        os.chmod(self.sock, mode)
        listener.listen(4)
        p = subprocess.Popen([sys.executable, "-I", "-c", HANDOVER, str(listener.fileno()), str(LAUNCHER), *self.args()], pass_fds=(listener.fileno(),),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmp)
        self.procs.append(p)
        return p

    def test_no_access_section_admits_nobody(self) -> None:
        self.write_toml("allow_taint_bits = [2]\n")
        self.handed_over()
        c = Client(self.sock)
        msg = c.read()
        self.assertEqual((msg["id"], msg["error"]["code"], msg["error"]["detail"]["check"]), (None, "REFUSED", "ACCESS_DENIED"))
        self.assertTrue(c.at_eof())
        c.close()

    def test_a_helper_toml_with_a_loose_mode_admits_nobody(self) -> None:
        os.chmod(self.toml, 0o666)
        self.handed_over()
        c = Client(self.sock)
        self.assertEqual(c.read()["error"]["detail"]["check"], "ACCESS_DENIED")
        c.close()

    def test_a_self_made_socket_with_a_loose_helper_toml_is_not_made(self) -> None:
        os.chmod(self.toml, 0o666)
        p = subprocess.Popen([sys.executable, "-I", str(LAUNCHER), *self.args()], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmp)
        self.procs.append(p)
        self.assertEqual(p.wait(10), 77)
        self.assertFalse(os.path.lexists(self.sock))

    def test_no_group_means_no_self_made_socket(self) -> None:
        self.write_toml(f'[access]\nusers = ["{my_name()}"]\n')
        p = subprocess.Popen([sys.executable, "-I", str(LAUNCHER), *self.args()], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmp)
        self.procs.append(p)
        self.assertEqual(p.wait(10), 77)
        self.assertIn(b"no [access] group", p.stderr.read())
        self.assertFalse(os.path.lexists(self.sock))

    def test_a_second_helper_is_refused_by_the_instance_lock_and_leaves_the_first_socket_alone(self) -> None:
        self.start()
        second = subprocess.Popen([sys.executable, "-I", str(LAUNCHER), *self.args()], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmp)
        self.procs.append(second)
        self.assertEqual(second.wait(10), 75)
        c = Client(self.sock)
        self.assertTrue(c.call("1", "preflight")["ok"])
        c.close()

    def test_socket_activation_handover_serves_and_idles_out(self) -> None:
        self.write_toml(f'idle_exit_s = 1\n[access]\nusers = ["{my_name()}"]\n')
        p = self.handed_over()
        c = Client(self.sock)  # the kernel queues the connect on the handed-over socket before the helper accepts
        self.assertTrue(c.call("1", "preflight")["ok"])
        c.close()
        self.assertEqual(p.wait(10), 0)  # idle for 1 s with no client: gone
        self.assertTrue(os.path.lexists(self.sock))  # systemd's node, not removed by the helper
        self.assertIn(b"idle for 1 s", p.stderr.read())

    def test_a_world_accessible_handover_is_refused_at_start(self) -> None:
        p = self.handed_over(0o666)
        self.assertEqual(p.wait(10), 77)
        self.assertIn(b"access to other users", p.stderr.read())

    def test_usage(self) -> None:
        for argv in (["--sim", "--socket-path", str(self.sock)], ["--sim", "--helper-toml", str(self.toml)]):
            with self.subTest(argv=argv):
                p = subprocess.run([sys.executable, "-I", str(LAUNCHER), *argv], stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
                self.assertEqual(p.returncode, 64)
                self.assertIn(b"need --socket", p.stderr)


if __name__ == "__main__":
    unittest.main()
