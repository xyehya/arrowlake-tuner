"""``--device-win``: argument exclusivity and wiring. The Windows port, gate and LP-map check are injected; nothing real is built."""
from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path
from unittest import mock

from arltuner import cli
from arltuner.guard import read_all
from arltuner.msr import SimConfig, SimMsrPort, WinThrottleStopMsrPort
from arltuner.profiles.fingerprint import FINGERPRINT_FILE
from arltuner.topology import TopologyMismatch

from .controls._support import sim_config
from .fixtures.win_reference import FUSED_POINT, LP_OF_CPU, FakeWin32, reference_fingerprint
from .test_cli import POLICY_TOML, CliCase, forbidden_port


class WinCli(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.gate_bits: list[frozenset[int]] = []
        self.ports = 0
        self.fingerprint_file = self.write(FINGERPRINT_FILE, reference_fingerprint().to_json("2026-10-12T00:00:00Z"))

    def win_port(self, fingerprint=None):  # type: ignore[no-untyped-def]
        self.ports += 1
        return self.sim

    def gate(self, bits):  # type: ignore[no-untyped-def]
        self.gate_bits.append(bits)
        return lambda phase: (True, "ok")

    def run_win(self, *argv: object, **kwargs):  # type: ignore[no-untyped-def]
        params = dict(win_port_factory=lambda fingerprint: self.win_port(fingerprint), win_health_gate_factory=self.gate, win_detect_fn=lambda fingerprint: fingerprint.topology())
        params.update(kwargs)
        if "--device-win" in argv and "--fingerprint" not in argv and "--no-fingerprint" not in argv:
            argv = (*argv, "--fingerprint", self.fingerprint_file)
        return self.run_cli(*[a for a in argv if a != "--no-fingerprint"], **params)

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
        def mismatch(fingerprint):
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

    def test_read_vf_says_it_runs_no_identity_check(self) -> None:
        code, _, err = self.run_win(*self.read_args(), stdin=self.confirmed())
        self.assertEqual(code, 0, err)
        self.assertIn("no identity check", err)

    def test_fingerprint_is_refused_without_device_win(self) -> None:
        code, _, err = self.run_cli("read-vf", "--sim", "--cpu", 4, "--fingerprint", self.fingerprint_file)
        self.assertEqual(code, 1, err)
        self.assertIn("--fingerprint only applies with --device-win", err)

    def test_the_default_fingerprint_path_is_the_accepted_dir_under_localappdata(self) -> None:
        accepted = self.tmp / "arltuner" / "accepted"
        accepted.mkdir(parents=True)
        (accepted / FINGERPRINT_FILE).write_text(reference_fingerprint().to_json("t"), encoding="utf-8")
        env = {"ARLTUNER_ALLOW_MAILBOX": "1", "USER": "tester", "LOCALAPPDATA": str(self.tmp)}
        code, _, err = self.run_win(*self.read_args(), "--no-fingerprint", stdin=self.confirmed(), env=env)
        self.assertEqual(code, 0, err)

    def test_missing_fingerprint_refuses_before_anything_is_sent(self) -> None:
        for env_extra, argv_extra, needle in (
            ({}, [], "pass --fingerprint FILE"),
            ({"LOCALAPPDATA": str(self.tmp / "nowhere")}, [], "re-accept on Linux to record the fingerprint"),
            ({}, ["--fingerprint", str(self.tmp / "gone.json")], "re-accept on Linux to record the fingerprint"),
        ):
            with self.subTest(needle=needle, extra=argv_extra):
                env = {"ARLTUNER_ALLOW_MAILBOX": "1", "USER": "tester", **env_extra}
                code, _, err = self.run_win(*self.read_args(), *argv_extra, "--no-fingerprint", stdin=self.confirmed(), env=env)
                self.assertEqual(code, 2, err)
                self.assertIn(needle, err)
        self.assertEqual(self.ports, 0)
        self.nothing_sent()

    def test_unusable_fingerprints_refuse_with_the_reason(self) -> None:
        cases = {
            "not json": "{",
            "no words": reference_fingerprint({}).to_json("t"),
            "no cpu identity": reference_fingerprint(cpu_id=None).to_json("t"),
            "duplicate P words": reference_fingerprint({**FUSED_POINT, 3: FUSED_POINT[2]}).to_json("t"),
        }
        for name, text in cases.items():
            with self.subTest(name):
                path = self.write("bad.json", text)
                code, _, err = self.run_win(*self.read_args(), "--fingerprint", path, stdin=self.confirmed())
                self.assertEqual(code, 2, err)
                self.assertIn("Nothing was sent", err)
        self.assertEqual(self.ports, 0)
        self.nothing_sent()

    def test_linux_device_path_is_unchanged(self) -> None:
        code, _, err = self.run_cli("read-vf", "--device", "--cpu", 4, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed(), env={}, device_port_factory=forbidden_port)
        self.assertEqual(code, 2, err)


def _not_installed(path: str):
    raise FileNotFoundError(path)  # G-106: a machine without /etc/arltuner/policy.toml (D-030: per-user paths)


class Defaults(unittest.TestCase):
    def test_default_audit_path_on_windows(self) -> None:
        env = {"LOCALAPPDATA": r"C:\Users\x\AppData\Local", "USERPROFILE": r"C:\Users\x"}
        self.assertEqual(cli.default_audit_path(env, windows=True), Path(env["LOCALAPPDATA"]) / "arltuner" / "audit.jsonl")
        self.assertEqual(cli.default_audit_path(env, windows=False, lstat_fn=_not_installed), Path(env["USERPROFILE"]) / ".local" / "state" / "arltuner" / "audit.jsonl")
        self.assertEqual(cli.default_audit_path({"HOME": "/h"}, windows=True), Path("/h/.local/state/arltuner/audit.jsonl"))

    def test_default_win_factories_build_the_real_things_lazily(self) -> None:
        fingerprint = reference_fingerprint()
        with mock.patch("arltuner.cli.WinThrottleStopMsrPort") as port:
            cli._default_win_port(fingerprint)
        port.assert_called_once_with(writable=True, fingerprint=fingerprint)
        with mock.patch("arltuner.health.make_whea_health_gate") as gate:
            cli._default_win_health_gate_factory(frozenset({3}))
        gate.assert_called_once_with(mock.ANY)
        with mock.patch("arltuner.cli.verify_machine", side_effect=OSError("x")):
            with self.assertRaises(TopologyMismatch):
                cli._default_win_detect(fingerprint)


if __name__ == "__main__":
    unittest.main()


class Identity(WinCli):
    """The identity check (fused V/F point, cmd 0x09 P1=0x11 P2=0, on every P-core and the first CPU of each E cluster) that --device-win runs before any write."""

    IDENTITY_CPUS = [*range(8), 8, 12, 16, 20]

    def sim_with(self, fused: dict[int, int]) -> SimMsrPort:
        cfg = dataclasses.replace(sim_config(), fused_point=fused)
        return SimMsrPort(cfg)

    def setUp(self) -> None:
        super().setUp()
        self.policy_file = self.write("nodwell.toml", POLICY_TOML.replace("min_dwell_s = 60.0", "min_dwell_s = 0.0"))  # the guard's wall-clock dwell is real

    def apply_win(self, **kwargs):  # type: ignore[no-untyped-def]
        args = ["apply", self.profile_file, "--device-win", "--policy", self.policy_file, "--audit", self.audit, "--lock", self.lock]
        return self.run_win(*args, stdin=self.confirmed(), sleep=lambda s: None, **kwargs)

    def lp_modelled(self, swapped: tuple[int, int] | None = None) -> None:
        """The real ``WinThrottleStopMsrPort`` over ``FakeWin32`` over the simulator. ``swapped`` = two Linux P-cores whose silicon sits on each other's LP."""
        truth = {lp: cpu for cpu, lp in LP_OF_CPU.items()}
        if swapped is not None:
            a, b = swapped
            truth[LP_OF_CPU[a]], truth[LP_OF_CPU[b]] = b, a
        self.api = FakeWin32(sim=self.sim, cpu_of_lp=truth)

        def build(fingerprint):  # type: ignore[no-untyped-def]
            self.ports += 1
            return WinThrottleStopMsrPort(True, api=self.api, fingerprint=fingerprint)

        self.win_port = build  # type: ignore[method-assign]

    def test_apply_matching_fused_points_proceeds_and_is_announced(self) -> None:
        self.sim = self.sim_with(dict(FUSED_POINT))
        code, out, err = self.apply_win()
        self.assertEqual(code, 0, out + err)
        self.assertIn("Identity check first", out)
        self.assertIn("12 guarded reads", out)
        self.assertIn("identity check passed", out)
        reads = [r for r in read_all(self.audit) if r["event"] == "attempt"]
        self.assertEqual([(r["cpu"], r["cmd"]) for r in reads[:12]], [(c, "0x09") for c in self.IDENTITY_CPUS])
        self.assertIn("provisional", reads[0]["purpose"])
        self.assertTrue(self.sim.writes)

    def test_a_shuffled_p_order_is_resolved_and_the_write_lands_on_the_right_core(self) -> None:
        self.sim = self.sim_with(dict(FUSED_POINT))
        self.lp_modelled(swapped=(4, 5))  # the profile tunes P-core 4; Windows' ascending LP order puts core 5 first
        code, out, err = self.apply_win()
        self.assertEqual(code, 0, out + err)
        self.assertIn("2 P-cores were in a different order", out)
        mailbox_writes = [r for r in self.sim.requests if r.cmd == 0x11]
        self.assertTrue(mailbox_writes)
        self.assertEqual({r.cpu for r in mailbox_writes}, {4})  # physical core 4, not the first guess (core 5)

    def test_the_reference_order_needs_no_rebind(self) -> None:
        self.sim = self.sim_with(dict(FUSED_POINT))
        self.lp_modelled()
        code, out, err = self.apply_win()
        self.assertEqual(code, 0, out + err)
        self.assertIn("(0 P-cores were in a different order", out)
        self.assertEqual({r.cpu for r in self.sim.requests if r.cmd == 0x11}, {4})

    def test_a_port_that_cannot_rebind_refuses_a_shuffled_order(self) -> None:
        fused = dict(FUSED_POINT)
        fused[2], fused[3] = fused[3], fused[2]
        self.sim = self.sim_with(fused)
        code, out, err = self.apply_win()
        self.assertEqual(code, 2, out + err)
        self.assertIn("cannot rebind", err)
        self.assertEqual({r.cmd for r in self.sim.requests}, {9})  # only the identity reads reached the machine
        self.assertEqual([r["cmd"] for r in read_all(self.audit) if r["event"] == "attempt"], ["0x09"] * 12)

    def test_duplicate_fused_words_refuse_and_nothing_is_written(self) -> None:
        fused = dict(FUSED_POINT)
        fused[6] = fused[1]
        self.sim = self.sim_with(fused)
        self.lp_modelled()
        code, out, err = self.apply_win()
        self.assertEqual(code, 2, out + err)
        self.assertIn("identity check failed", err)
        self.assertIn("cannot be told apart", err)
        self.assertEqual({r.cmd for r in self.sim.requests}, {9})

    def test_a_word_that_is_not_recorded_refuses(self) -> None:
        fused = dict(FUSED_POINT)
        fused[7] = 0x12345
        self.sim = self.sim_with(fused)
        self.lp_modelled()
        code, _, err = self.apply_win()
        self.assertEqual(code, 2, err)
        self.assertIn("no P-core of the accepted state recorded", err)
        self.assertEqual({r.cmd for r in self.sim.requests}, {9})

    def test_an_e_cluster_with_another_word_refuses(self) -> None:
        fused = dict(FUSED_POINT)
        fused[16] = FUSED_POINT[8]
        self.sim = self.sim_with(fused)
        self.lp_modelled()
        code, _, err = self.apply_win()
        self.assertEqual(code, 2, err)
        self.assertIn("E cluster 2", err)
        self.assertEqual({r.cmd for r in self.sim.requests}, {9})

    def test_policy_without_the_identity_read_refuses_before_sending(self) -> None:
        policy = self.write("no09.toml", POLICY_TOML.replace("min_dwell_s = 60.0", "min_dwell_s = 0.0").replace("cmd = 0x09\np1 = [0x11]", "cmd = 0x09\np1 = [0x12]"))
        self.sim = self.sim_with(dict(FUSED_POINT))
        args = ["apply", self.profile_file, "--device-win", "--policy", policy, "--audit", self.audit, "--lock", self.lock]
        code, _, err = self.run_win(*args, stdin=self.confirmed(), sleep=lambda s: None)
        self.assertEqual(code, 2, err)
        self.assertIn("identity check", err)
        self.nothing_sent()
        self.assertEqual(self.ports, 0)

    def test_the_identity_reads_take_their_share_of_the_session_budget(self) -> None:
        tight = self.write("tight.toml", POLICY_TOML.replace("min_dwell_s = 60.0", "min_dwell_s = 0.0").replace("max_operations_per_session = 60", "max_operations_per_session = 12"))
        self.sim = self.sim_with(dict(FUSED_POINT))
        args = ["apply", self.profile_file, "--device-win", "--policy", tight, "--audit", self.audit, "--lock", self.lock]
        code, _, err = self.run_win(*args, stdin=self.confirmed(), sleep=lambda s: None)
        self.assertEqual(code, 2, err)
        self.assertIn("12 guard operations", err)
        self.nothing_sent()

    def test_vf_dump_win_runs_the_identity_check_first(self) -> None:
        fused = dict(FUSED_POINT)
        fused[0], fused[1] = fused[1], fused[0]
        self.sim = self.sim_with(fused)
        wide = self.write("wide.toml", POLICY_TOML.replace("min_dwell_s = 60.0", "min_dwell_s = 0.0").replace("cmd = 0x10\np1 = [0]\np2 = [0]", "cmd = 0x10\np1 = [0]\np2 = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]"))
        args = ["vf-dump", "--device-win", "--cpus", "0", "--policy", wide, "--audit", self.audit, "--lock", self.lock]
        code, _, err = self.run_win(*args, stdin=self.confirmed(), sleep=lambda s: None)
        self.assertEqual(code, 2, err)
        self.assertIn("cannot rebind", err)
        self.assertEqual({r.cmd for r in self.sim.requests}, {9})

    def test_linux_device_path_has_no_identity_check(self) -> None:
        self.assertNotIn("Identity check", self.run_cli("read-vf", "--device", "--cpu", 4, "--audit", self.audit, "--lock", self.lock, stdin="CONFIRM x\n", device_health_gate_factory=lambda b: None, device_port_factory=forbidden_port)[1])

    def test_point_offset_win_shuffled_pair_is_resolved_before_the_baseline_read(self) -> None:
        self.sim = SimMsrPort(SimConfig.measured(per_point=True, reject_zero_fields=True))  # per-point words with the reference fused words
        self.lp_modelled(swapped=(4, 5))
        points = Path(__file__).resolve().parents[1] / "examples" / "reference-285k" / "policies" / "session-2026-10-02-points.toml"
        policy = self.write("pts.toml", points.read_text(encoding="utf-8").replace("min_dwell_s", "min_dwell_s = 0.0 #", 1))
        args = ["point-offset", "--device-win", "--cpu", 4, "--p2", 1, "--offset-mv", -5, "--policy", policy, "--audit", self.audit, "--lock", self.lock]
        code, out, err = self.run_win(*args, stdin=self.confirmed(), sleep=lambda s: None)
        self.assertEqual(code, 0, out + err)
        self.assertEqual([r.cmd for r in self.sim.requests[:12]], [9] * 12)
        self.assertEqual({r.cpu for r in self.sim.requests if r.cmd in (0x10, 0x11)}, {4})  # the baseline read and the write hit physical core 4
