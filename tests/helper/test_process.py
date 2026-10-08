"""The launcher as a real process (``tools/arltuner-helper.py --sim``): stdin/stdout wiring, EOF, real SIGTERM / SIGHUP, startup refusals.
Simulator and temp paths only; nothing is installed, no sudo, no device."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "tools" / "arltuner-helper.py"
POSIX = os.name == "posix"


class ProcessCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.procs: list[subprocess.Popen[bytes]] = []
        self.addCleanup(self._reap)

    def _reap(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            for f in (p.stdin, p.stdout, p.stderr):
                if f:
                    f.close()

    def spawn(self, *extra: str) -> subprocess.Popen[bytes]:
        p = subprocess.Popen(
            [sys.executable, "-I", str(LAUNCHER), "--sim", "--audit", str(self.tmp / "audit.jsonl"), "--lock", str(self.tmp / "lock"), *extra],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmp,
        )
        self.procs.append(p)
        return p

    @staticmethod
    def roundtrip(p: subprocess.Popen[bytes], req: dict) -> dict:
        p.stdin.write(json.dumps(req).encode() + b"\n")
        p.stdin.flush()
        while True:
            msg = json.loads(p.stdout.readline())
            if msg.get("id") == req["id"]:
                return msg


@unittest.skipUnless(POSIX, "the helper is Linux-only (flock, signals)")
class ProcessTests(ProcessCase):
    def test_request_response_then_eof_exits_zero(self) -> None:
        p = self.spawn()
        self.assertTrue(self.roundtrip(p, {"id": "1", "op": "preflight"})["ok"])
        p.stdin.close()
        self.assertEqual(p.wait(10), 0)
        self.assertIn(b"EOF", p.stderr.read())  # stderr is the log, stdout stayed pure protocol

    def test_sigterm_and_sighup_stop_an_idle_helper_cleanly(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(sig=sig.name):
                p = self.spawn()
                self.assertTrue(self.roundtrip(p, {"id": "1", "op": "preflight"})["ok"])
                p.send_signal(sig)
                self.assertEqual(p.wait(10), 0)
                self.assertNotIn(b"Traceback", p.stderr.read())

    def test_sigterm_during_a_dump_stops_at_a_safe_point(self) -> None:
        policy = self.tmp / "policy.toml"
        from ._support import POLICY_TOML

        policy.write_text(POLICY_TOML, encoding="utf-8", newline="\n")
        p = self.spawn("--policy", str(policy))
        p.stdin.write(json.dumps({"id": "d", "op": "vf.dump", "args": {"all_e": True}}).encode() + b"\n")
        p.stdin.flush()
        seen_progress = False
        while not seen_progress:
            msg = json.loads(p.stdout.readline())
            seen_progress = msg.get("event") == "progress"
        p.send_signal(signal.SIGTERM)
        final = None
        for raw in p.stdout:
            msg = json.loads(raw)
            if msg.get("id") == "d":
                final = msg
        self.assertEqual(p.wait(10), 0)
        # either the dump finished before the signal landed or it was stopped between transactions; never a crash or a missing answer
        self.assertIsNotNone(final)
        self.assertTrue(final["ok"] or final["error"]["code"] == "CANCELLED", final)

    def test_a_second_process_on_the_same_lock_refuses_to_start(self) -> None:
        first = self.spawn()
        self.assertTrue(self.roundtrip(first, {"id": "1", "op": "preflight"})["ok"])
        second = self.spawn()
        msg = json.loads(second.stdout.readline())
        self.assertEqual((msg["ok"], msg["error"]["code"]), (False, "BUSY"))
        self.assertEqual(second.wait(10), 75)
        self.assertTrue(self.roundtrip(first, {"id": "2", "op": "preflight"})["ok"])

    def test_heartbeat_arrives_on_the_two_second_clock(self) -> None:
        p = self.spawn()
        t0 = time.monotonic()
        while True:
            msg = json.loads(p.stdout.readline())
            if msg.get("event") == "heartbeat":
                break
        self.assertGreaterEqual(time.monotonic() - t0, 1.5)
        self.assertLess(time.monotonic() - t0, 4.0)
        self.assertEqual((msg["state"], msg["op_id"]), ("idle", None))

    def test_usage_errors(self) -> None:
        for argv in ([], ["--sim", "--device"], ["--device"]):
            p = subprocess.run([sys.executable, "-I", str(LAUNCHER), *argv], input=b"", capture_output=True, cwd=self.tmp, timeout=30)
            self.assertEqual(p.returncode, 64, (argv, p.stderr))
            self.assertEqual(p.stdout, b"")

    def test_device_mode_refuses_to_start_from_a_checkout_or_without_root(self) -> None:
        # never reaches a device: the startup refusal (not root / code tree not root-owned) comes first, and this test would not run as root on hardware either
        audit, policy = self.tmp / "a.jsonl", self.tmp / "p.toml"
        p = subprocess.run([sys.executable, "-I", str(LAUNCHER), "--device", "--audit", str(audit), "--policy", str(policy), "--lock", str(self.tmp / "l")],
                           input=b"", capture_output=True, cwd=self.tmp, timeout=30)
        self.assertEqual(p.returncode, 77, p.stderr)
        msg = json.loads(p.stdout.splitlines()[0])
        self.assertEqual((msg["id"], msg["error"]["code"], msg["error"]["detail"]["check"]), (None, "REFUSED", "STARTUP"))
        self.assertFalse(audit.exists())


if __name__ == "__main__":
    unittest.main()
