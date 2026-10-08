"""``--device-win``: argument exclusivity and wiring. The Windows port, gate and LP-map check are injected; nothing real is built."""
from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path
from unittest import mock

from arltuner import cli
from arltuner.guard import read_all
from arltuner.msr import FUSED_POINT, SimMsrPort
from arltuner.topology import TopologyMismatch, arl_s_285k

from .controls._support import sim_config
from .test_cli import POLICY_TOML, CliCase, forbidden_port


class WinCli(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.gate_bits: list[frozenset[int]] = []
        self.ports = 0

    def win_port(self):  # type: ignore[no-untyped-def]
        self.ports += 1
        return self.sim

    def gate(self, bits):  # type: ignore[no-untyped-def]
        self.gate_bits.append(bits)
        return lambda phase: (True, "ok")

    def run_win(self, *argv: object, **kwargs):  # type: ignore[no-untyped-def]
        params = dict(win_port_factory=self.win_port, win_health_gate_factory=self.gate, win_detect_fn=arl_s_285k)
        params.update(kwargs)
        return self.run_cli(*argv, **params)

    def read_args(self, *extra: object) -> list[object]:
        return ["read-vf", "--device-win", "--cpu", 4, "--audit", self.audit, "--lock", self.lock, *extra]

    def test_exclusive_with_device_and_sim(self) -> None:
        for other in ("--device", "--sim"):
            for cmd in (["apply", self.profile_file], ["read-vf", "--cpu", 4], ["read", "--cpu", 4, "--cmd", 0x10], ["vf-dump"], ["point-offset", "--cpu", 0, "--p2", 1, "--offset-mv", -5]):
                with self.subTest(cmd=cmd[0], other=other):
                    self.assertEqual(self.run_win(*cmd, "--device-win", other)[0], 1)
        self.assertEqual(self.ports, 0)

    def test_apply_requires_a_mode(self) -> None:
        self.assertEqual(self.run_win("apply", self.profile_file)[0], 1)

    def test_plan_has_no_device_win(self) -> None:
        self.assertEqual(self.run_win("plan", self.profile_file, "--device-win")[0], 1)

    def test_env_flag_and_confirmation_still_gate_the_port(self) -> None:
        code, _, err = self.run_win(*self.read_args(), env={}, stdin=self.confirmed())
        self.assertEqual(code, 2, err)
        self.assertIn("ARLTUNER_ALLOW_MAILBOX", err)
        code, _, err = self.run_win(*self.read_args(), stdin="CONFIRM nope\n")
        self.assertEqual(code, 2, err)
        self.assertEqual(self.ports, 0)

    def test_topology_check_refuses_before_the_port(self) -> None:
        def mismatch():
            raise TopologyMismatch("the LP map does not match")

        code, _, err = self.run_win(*self.read_args(), stdin=self.confirmed(), win_detect_fn=mismatch)
        self.assertEqual(code, 2, err)
        self.assertIn("does not match", err)
        self.assertEqual(self.ports, 0)

    def test_missing_gate_refuses_before_the_port(self) -> None:
        code, _, err = self.run_win(*self.read_args(), stdin=self.confirmed(), win_health_gate_factory=lambda bits: None)
        self.assertEqual(code, 2, err)
        self.assertIn("no health gate", err)
        self.assertEqual(self.ports, 0)

    def test_read_vf_goes_through_the_win_port_and_gate(self) -> None:
        code, out, err = self.run_win(*self.read_args(), stdin=self.confirmed(), device_health_gate_factory=lambda b: self.fail("linux gate"))
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.ports, 1)
        self.assertEqual(self.gate_bits, [frozenset()])
        self.assertNotIn("simulator", out)

    def test_allow_taint_bit_is_accepted_and_ignored_with_a_note(self) -> None:
        code, _, err = self.run_win(*self.read_args("--allow-taint-bit", 5), stdin=self.confirmed())
        self.assertEqual(code, 0, err)
        self.assertIn("ignored with --device-win", err)

    def test_vf_dump_measure_is_refused(self) -> None:
        code, _, err = self.run_win("vf-dump", "--device-win", "--measure", "--audit", self.audit, "--lock", self.lock, "--policy", self.policy_file)
        self.assertEqual(code, 2, err)
        self.assertIn("--device-win", err)
        self.assertEqual(self.ports, 0)

    def test_linux_device_path_is_unchanged(self) -> None:
        code, _, err = self.run_cli("read-vf", "--device", "--cpu", 4, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed(), env={}, device_port_factory=forbidden_port)
        self.assertEqual(code, 2, err)


class Defaults(unittest.TestCase):
    def test_default_audit_path_on_windows(self) -> None:
        env = {"LOCALAPPDATA": r"C:\Users\x\AppData\Local", "USERPROFILE": r"C:\Users\x"}
        self.assertEqual(cli.default_audit_path(env, windows=True), Path(env["LOCALAPPDATA"]) / "linuxmon" / "audit.jsonl")
        self.assertEqual(cli.default_audit_path(env, windows=False), Path(env["USERPROFILE"]) / ".local" / "state" / "linuxmon" / "audit.jsonl")
        self.assertEqual(cli.default_audit_path({"HOME": "/h"}, windows=True), Path("/h/.local/state/linuxmon/audit.jsonl"))

    def test_default_win_factories_build_the_real_things_lazily(self) -> None:
        with mock.patch("arltuner.cli.WinThrottleStopMsrPort") as port:
            cli._default_win_port()
        port.assert_called_once_with(writable=True)
        with mock.patch("arltuner.health.make_whea_health_gate") as gate:
            cli._default_win_health_gate_factory(frozenset({3}))
        gate.assert_called_once_with(mock.ANY)
        with mock.patch("arltuner.cli.verify_machine", side_effect=OSError("x")):
            with self.assertRaises(TopologyMismatch):
                cli._default_win_detect()


if __name__ == "__main__":
    unittest.main()


class Identity(WinCli):
    """The P-core identity check (fused V/F point, cmd 0x09 P1=0x11 P2=0) that --device-win runs before any write."""

    def sim_with(self, fused: dict[int, int]) -> SimMsrPort:
        cfg = dataclasses.replace(sim_config(), fused_point=fused)
        return SimMsrPort(cfg)

    def setUp(self) -> None:
        super().setUp()
        self.policy_file = self.write("nodwell.toml", POLICY_TOML.replace("min_dwell_s = 60.0", "min_dwell_s = 0.0"))  # the guard's wall-clock dwell is real

    def apply_win(self, **kwargs):  # type: ignore[no-untyped-def]
        args = ["apply", self.profile_file, "--device-win", "--policy", self.policy_file, "--audit", self.audit, "--lock", self.lock]
        return self.run_win(*args, stdin=self.confirmed(), sleep=lambda s: None, **kwargs)

    def test_apply_matching_fused_points_proceeds_and_is_announced(self) -> None:
        self.sim = self.sim_with(dict(FUSED_POINT))
        code, out, err = self.apply_win()
        self.assertEqual(code, 0, out + err)
        self.assertIn("Identity check first", out)
        self.assertIn("identity check passed", out)
        reads = [r for r in read_all(self.audit) if r["event"] == "attempt"]
        self.assertEqual([(r["cpu"], r["cmd"]) for r in reads[:8]], [(c, "0x09") for c in range(8)])
        self.assertTrue(self.sim.writes)

    def test_apply_swapped_p_cores_refused_and_nothing_written(self) -> None:
        fused = dict(FUSED_POINT)
        fused[2], fused[3] = fused[3], fused[2]
        self.sim = self.sim_with(fused)
        code, out, err = self.apply_win()
        self.assertEqual(code, 2, out + err)
        self.assertIn("identity check failed on cpu 2", err)
        self.assertIn("LP map", err)
        self.assertEqual({r.cmd for r in self.sim.requests}, {9})  # only the identity reads reached the machine
        self.assertEqual([r["cmd"] for r in read_all(self.audit) if r["event"] == "attempt"], ["0x09"] * 3)  # stopped at the first mismatch

    def test_policy_without_the_identity_read_refuses_before_sending(self) -> None:
        policy = self.write("no09.toml", POLICY_TOML.replace("min_dwell_s = 60.0", "min_dwell_s = 0.0").replace("cmd = 0x09\np1 = [0x11]", "cmd = 0x09\np1 = [0x12]"))
        self.sim = self.sim_with(dict(FUSED_POINT))
        args = ["apply", self.profile_file, "--device-win", "--policy", policy, "--audit", self.audit, "--lock", self.lock]
        code, _, err = self.run_win(*args, stdin=self.confirmed(), sleep=lambda s: None)
        self.assertEqual(code, 2, err)
        self.assertIn("identity check", err)
        self.nothing_sent()
        self.assertEqual(self.ports, 0)

    def test_vf_dump_win_runs_the_identity_check_first(self) -> None:
        fused = dict(FUSED_POINT)
        fused[0], fused[1] = fused[1], fused[0]
        self.sim = self.sim_with(fused)
        wide = self.write("wide.toml", POLICY_TOML.replace("min_dwell_s = 60.0", "min_dwell_s = 0.0").replace("cmd = 0x10\np1 = [0]\np2 = [0]", "cmd = 0x10\np1 = [0]\np2 = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]"))
        args = ["vf-dump", "--device-win", "--cpus", "0", "--policy", wide, "--audit", self.audit, "--lock", self.lock]
        code, _, err = self.run_win(*args, stdin=self.confirmed(), sleep=lambda s: None)
        self.assertEqual(code, 2, err)
        self.assertIn("identity check failed on cpu 0", err)
        self.assertEqual({r.cmd for r in self.sim.requests}, {9})

    def test_linux_device_path_has_no_identity_check(self) -> None:
        self.assertNotIn("Identity check", self.run_cli("read-vf", "--device", "--cpu", 4, "--audit", self.audit, "--lock", self.lock, stdin="CONFIRM x\n", device_health_gate_factory=lambda b: None, device_port_factory=forbidden_port)[1])

    def test_point_offset_win_swapped_pair_refused_before_the_baseline_read(self) -> None:
        fused = dict(FUSED_POINT)
        fused[4], fused[5] = fused[5], fused[4]
        self.sim = self.sim_with(fused)
        points = Path(__file__).resolve().parents[1] / "policies" / "session-2026-10-02-points.toml"
        policy = self.write("pts.toml", points.read_text(encoding="utf-8").replace("min_dwell_s", "min_dwell_s = 0.0 #", 1))
        args = ["point-offset", "--device-win", "--cpu", 0, "--p2", 1, "--offset-mv", -5, "--policy", policy, "--audit", self.audit, "--lock", self.lock]
        code, out, err = self.run_win(*args, stdin=self.confirmed(), sleep=lambda s: None)
        self.assertEqual(code, 2, out + err)
        self.assertIn("identity check failed on cpu 4", err)
        self.assertEqual({r.cmd for r in self.sim.requests}, {9})
