"""T-014 CLI tests: ``arltuner.cli.main`` against ``SimMsrPort`` with temp files and injected I/O.

Rules under test: docs/tasks/T-014-controls-cli.md ("CLI", "Tests"), docs/SAFETY.md ("OC mailbox rule", "Write-session protocol").
**No test touches a real device**: every ``--device`` test exercises only a refusal, and ``arltuner.cli.DevCpuMsrPort`` is replaced
by a mock that must never be called (``device_port_factory`` is left at its default on purpose, so a leak would hit the mock, not
``/dev/cpu``). ``detect_fn`` is always injected, so nothing reads the real ``/sys``.
"""
from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arltuner import cli
from arltuner.guard import ENV_FLAG, WriteGuard, read_all
from arltuner.mailbox import VfWord
from arltuner.msr import SimConfig, SimMsrPort, SimRequest
from arltuner.topology import TopologyMismatch, arl_s_285k

from arltuner.controls import XTU_SHAPE_CAVEAT

from .controls._support import P4_BASELINE, e_core, p4, profile_text, sim_config

TOKEN = "tok-cli-7"
ENV_OK = {ENV_FLAG: "1", "USER": "tester"}

POLICY_TOML = """\
cpus = ["0-23"]
max_operations_per_session = 60
max_operations_per_boot = 200
min_dwell_s = 60.0
max_ratio_step = 8        # these tests are not about the D-016 step limits: relaxed to their sanity maxima
max_v_step_mv = 200
max_offset_step_mv = 200
assumed_bios_offset_mv = { P = -50, E = 0 }   # these tests exercise the D-015 mechanism with a stated BIOS offset (the shipped default is 0 for both)

[[allow]]
name = "misc_read"
cmd = 0x14
p1 = [0]
p2 = [0]
kind = "read"

[[allow]]
name = "vf_read"
cmd = 0x10
p1 = [0]
p2 = [0]
kind = "read"

[[allow]]
name = "misc_write"
cmd = 0x15
p1 = [0]
p2 = [0]
kind = "write"
check = "percore_enable_only"

[[allow]]
name = "vf_write"
cmd = 0x11
p1 = [0]
p2 = [0]
kind = "write"
check = "vf_bounded"

[[allow]]
name = "favored_read"
cmd = 0x1C
p1 = [0]
p2 = [0]
kind = "read"

[[allow]]
name = "fused_read"
cmd = 0x09
p1 = [0x11]
p2 = [0]
kind = "read"

[[allow]]
name = "granular_read"
cmd = 0x26
p1 = [0]
p2 = [0]
kind = "read"

[[allow]]
name = "max_voltage_read"
cmd = 0x28
p1 = [0]
p2 = [0]
kind = "read"

[[allow]]
name = "favored_write"
cmd = 0x1D
p1 = [0]
p2 = [0]
kind = "write"
check = "favored_ratio_follows_vf"

[[allow]]
name = "granular_write"
cmd = 0x27
p1 = [0]
p2 = [0]
kind = "write"
check = "granular_equals_read"
"""


def forbidden_port() -> object:
    raise AssertionError("a device port must never be constructed in a test")


def not_installed(path: str):
    """An ``lstat`` for a machine without ``/etc/arltuner`` (D-030: the CLI then keeps the legacy lock / audit paths)."""
    raise FileNotFoundError(path)


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.audit = self.tmp / "audit.jsonl"
        self.lock = self.tmp / "mailbox.lock"
        self.policy_file = self.write("policy.toml", POLICY_TOML)
        self.profile_file = self.write("p4.toml", profile_text(p4(offset_mv=-30, ratio=55)))
        self.sim = SimMsrPort(sim_config())  # the old SIM-ONLY defaults with the unmeasured rule on; measured(): see Measured below

    def write(self, name: str, text: str) -> Path:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def run_cli(self, *argv: object, env=None, stdin: str = "", sim: SimMsrPort | None = None, token: str = TOKEN, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        if sim is not None:
            self.sim = sim
        params = dict(
            env=ENV_OK if env is None else env,
            stdin=io.StringIO(stdin),
            stdout=out,
            stderr=err,
            token_fn=lambda: token,
            boot_id_fn=lambda: "test-boot",  # T-016: never read the real /proc in a test
            sim_factory=lambda: self.sim,
            detect_fn=arl_s_285k,
        )
        if "--device" not in argv:
            params["device_port_factory"] = forbidden_port
        params.update(kwargs)
        code = cli.main([str(a) for a in argv], **params)
        return code, out.getvalue(), err.getvalue()

    def apply_args(self, *extra: object) -> list[object]:
        return ["apply", self.profile_file, "--sim", "--policy", self.policy_file, "--audit", self.audit, "--lock", self.lock, *extra]

    def confirmed(self) -> str:
        return f"CONFIRM {TOKEN}\n"

    def nothing_sent(self) -> None:
        self.assertEqual(self.sim.writes, [])
        self.assertEqual(self.sim.reads, [])
        self.assertEqual(self.sim.requests, [])


class Topology(CliCase):
    def test_table(self) -> None:
        code, out, err = self.run_cli("topology")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(len(lines), 25)  # header + 24 cores
        self.assertIn("P-Core 4", out)
        self.assertIn("E-Core 12", out)
        row = next(ln for ln in lines if ln.startswith("E-Core 12 "))
        self.assertEqual(row.split()[2:4], ["20", "3"])  # E-Core 12 = Linux CPU 20, cluster 3
        self.assertEqual([ln.split()[0:2] for ln in lines if ln.rstrip().endswith("yes")], [["P-Core", "0"], ["P-Core", "3"]])
        self.nothing_sent()

    def test_json(self) -> None:
        code, out, _ = self.run_cli("topology", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 24)
        self.assertEqual(rows[4], {"xtu_name": "P-Core 4", "kind": "P", "index": 4, "cpu": 4, "cluster": None, "favored": False})
        self.assertEqual(rows[20], {"xtu_name": "E-Core 12", "kind": "E", "index": 12, "cpu": 20, "cluster": 3, "favored": False})
        self.assertEqual([r["cpu"] for r in rows if r["favored"]], [0, 3])

    def test_needs_no_environment_flag(self) -> None:
        code, _, _ = self.run_cli("topology", env={})
        self.assertEqual(code, 0)


class Plan(CliCase):
    def test_allowed_plan_exits_0_and_sends_nothing(self) -> None:
        code, out, err = self.run_cli("plan", self.profile_file, "--sim", "--policy", self.policy_file)
        self.assertEqual((code, err), (0, ""), out)
        for needle in ("0x14", "0x10", "0x1C", "0x09", "0x26", "0x15", "0x11", "0x1D", "0x27", "Operations required: 13 mailbox WRMSRs = 9 guard operations", "4 read-backs", "policy allows 60", "ALLOWED",
                       "offset +0.00 -> -30.27 mV", "runtime offset -30.27 mV, effective -80.27 mV (assumed BIOS offset -50 mV)", "nothing was sent",
                       "WARNING: " + XTU_SHAPE_CAVEAT):
            self.assertIn(needle, out)
        self.nothing_sent()  # not even a mailbox read: the baseline comes from the simulator's own state
        self.assertFalse(self.audit.exists())

    def test_default_policy_needs_a_higher_limit_exit_2(self) -> None:
        code, out, _ = self.run_cli("plan", self.profile_file, "--sim")
        self.assertEqual(code, 2)
        self.assertIn("Operations required: 13 mailbox WRMSRs = 9 guard operations", out)
        self.assertIn("policy allows 1", out)
        self.assertIn("NEEDS A HIGHER LIMIT", out)
        self.assertIn("NOT ALLOWED", out)
        self.nothing_sent()

    def test_predicted_refusal_exits_2(self) -> None:
        for name, entry in {
            "overvolt": e_core(0, oc_voltage_mv=1500),  # G-124: a V target only exists on an OC point (E ratio 48 > 46); P4 at 55x has none
            "ratio up": p4(ratio=57),
            "offset -150": p4(offset_mv=-150),
            "offset -51 (P-core runtime floor, D-015)": p4(offset_mv=-51),
        }.items():
            with self.subTest(name):
                path = self.write("bad.toml", profile_text(entry))
                code, out, _ = self.run_cli("plan", path, "--sim", "--policy", self.policy_file)
                self.assertEqual(code, 2)
                self.assertIn("CHECK_FAILED", out)
                self.assertIn("NOT ALLOWED", out)
                self.nothing_sent()

    def test_the_baseline_comes_from_the_simulator_state(self) -> None:
        sim = SimMsrPort(sim_config(initial_vf={4: P4_BASELINE.with_(static=True)}))
        code, out, _ = self.run_cli("plan", self.profile_file, "--sim", "--policy", self.policy_file, sim=sim)
        self.assertEqual(code, 2)
        self.assertIn("static", out)

    def test_per_core_state_changes_the_step_count(self) -> None:
        sim = SimMsrPort(sim_config(misc_global_config=0x8))
        code, out, _ = self.run_cli("plan", self.profile_file, "--sim", "--policy", self.policy_file, sim=sim)
        self.assertEqual(code, 0)
        self.assertIn("Operations required: 11 mailbox WRMSRs = 8 guard operations", out)
        self.assertNotIn("cmd 0x15", out)

    def test_usage_errors_exit_1(self) -> None:
        self.assertEqual(self.run_cli("plan", self.profile_file)[0], 1)  # needs --sim
        self.assertEqual(self.run_cli("plan", self.tmp / "missing.toml", "--sim")[0], 1)
        bad = self.write("bad.toml", "this is not toml = = =\n")
        code, _, err = self.run_cli("plan", bad, "--sim")
        self.assertEqual(code, 1)
        self.assertIn("profile error", err)
        self.assertEqual(self.run_cli("plan", self.profile_file, "--sim", "--policy", self.tmp / "missing.toml")[0], 1)
        self.assertEqual(self.run_cli("plan", self.profile_file, "--device")[0], 1)  # plan never touches hardware
        self.nothing_sent()


class Apply(CliCase):
    def test_happy_path_against_the_simulator(self) -> None:
        env = dict(ENV_OK)
        code, out, err = self.run_cli(*self.apply_args(), stdin=self.confirmed(), env=env)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(env, ENV_OK)  # the CLI never writes the environment
        self.assertIn(f"CONFIRM {TOKEN}", out)  # the plan and the token were shown before anything was sent
        self.assertLess(out.index("Operations required"), out.index("CONFIRM"))
        self.assertLess(out.index("CONFIRM"), out.index("Apply report"))
        self.assertIn("Result: OK", out)
        self.assertIn("read-back equal", out)
        self.assertIn("Revert plan", out)
        self.assertEqual(self.sim.vf(4), P4_BASELINE.with_(offset_raw=-31, v_target_raw=0))  # G-120: no OC point at ratio 55
        self.assertTrue(self.sim.per_core_enabled)
        events = [r["event"] for r in read_all(self.audit)]
        self.assertEqual((events.count("attempt"), events.count("readback")), (9, 4))
        self.assertEqual({r["operator"] for r in read_all(self.audit)}, {"tester"})
        self.assertFalse(self.lock.exists())
        self.assertIn("dwell: waiting", out)  # the virtual clock honoured the 60 s dwell without really waiting

    def test_operator_option(self) -> None:
        self.run_cli(*self.apply_args("--operator", "yk"), stdin=self.confirmed())
        self.assertEqual({r["operator"] for r in read_all(self.audit)}, {"yk"})

    def test_ctrl_c_during_a_dwell_wait_reports_what_was_sent_and_never_continues(self) -> None:
        now = [0.0]
        waits: list[float] = []

        def sleep(seconds: float) -> None:
            if seconds >= 30:  # a dwell wait, not a transport busy-poll
                waits.append(seconds)
                if len(waits) == 6:  # the wait before the 0x11 write (the 7th operation), after the 0x15 write was executed
                    raise KeyboardInterrupt
            now[0] += seconds

        code, out, err = self.run_cli(*self.apply_args(), stdin=self.confirmed(), clock=lambda: now[0], sleep=sleep)
        self.assertEqual(code, 3)
        self.assertIn("interrupted", err)
        self.assertIn("Revert plan", err)
        self.assertIn("cmd 0x15", err)  # the executed 0x15 write is listed for the human
        self.assertNotIn("Apply report", out)
        self.assertEqual([r.cmd for r in self.sim.requests if r.cmd == 0x11], [])  # never continued
        self.assertFalse(self.lock.exists())

    def test_xtu_shape_caveat_and_revert_note_are_printed(self) -> None:
        path = self.write("ratio.toml", profile_text(p4(offset_mv=-30, ratio=50)))
        args = self.apply_args()
        args[1] = path
        code, out, _ = self.run_cli(*args, stdin=self.confirmed())
        self.assertEqual(code, 0, out)
        self.assertIn(XTU_SHAPE_CAVEAT, out)
        self.assertIn("a reboot restores the BIOS-programmed state; a write cannot clear an OC ratio above the top fused ratio", out)
        self.assertEqual(self.sim.vf(4).ratio, 50)
        self.assertEqual(self.sim.favored_word(4) & 0xFF, 50)  # 0x1D carried the ratio

    def test_confirm_fn_is_injectable_and_sees_the_prompt_with_the_token(self) -> None:
        seen: list[str] = []

        def confirm_fn(prompt: str) -> str:
            seen.append(prompt)
            return "CONFIRM " + re.search(r"CONFIRM (\S+)", prompt).group(1) + "\n"

        code, out, _ = self.run_cli(*self.apply_args(), confirm_fn=confirm_fn, token="random-abc")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(seen), 1)
        self.assertIn("CONFIRM random-abc", seen[0])

    def test_the_default_token_is_random(self) -> None:
        tokens = set()
        for _ in range(3):
            out = io.StringIO()
            cli.main([str(a) for a in self.apply_args()], env=ENV_OK, stdin=io.StringIO(""), stdout=out, stderr=io.StringIO(),
                     sim_factory=SimMsrPort, device_port_factory=forbidden_port, detect_fn=arl_s_285k)
            tokens.add(re.search(r"CONFIRM (\S+)", out.getvalue()).group(1))
        self.assertEqual(len(tokens), 3)

    def test_wrong_token_is_refused_and_nothing_is_sent(self) -> None:
        for typed in ("CONFIRM nope\n", "nope\n", f"{TOKEN}\n", "CONFIRM\n", f"CONFIRM {TOKEN} extra\n", "confirm " + TOKEN + "\n", "\n", ""):
            with self.subTest(typed=typed):
                code, out, err = self.run_cli(*self.apply_args(), stdin=typed)
                self.assertEqual(code, 2)
                self.assertIn("confirmation does not match", err)
                self.nothing_sent()
                self.assertEqual(self.sim.vf(4), P4_BASELINE)
                self.assertFalse(self.audit.exists())  # refused before the audit log / guard exist

    def test_env_flag_absent_is_refused_before_the_prompt(self) -> None:
        for env in ({}, {ENV_FLAG: "0"}, {ENV_FLAG: "true"}, {ENV_FLAG: ""}):
            with self.subTest(env=env):
                code, out, err = self.run_cli(*self.apply_args(), stdin=self.confirmed(), env=env)
                self.assertEqual(code, 2)
                self.assertIn(ENV_FLAG, err)
                self.assertNotIn("CONFIRM", out)  # no prompt, no token shown
                self.nothing_sent()

    def test_the_token_cannot_be_given_as_an_argument(self) -> None:
        for opt in ("--token", "--confirm"):
            with self.subTest(opt=opt):
                code, _, err = self.run_cli(*self.apply_args(opt, TOKEN), stdin=self.confirmed())
                self.assertEqual(code, 1)
                self.assertIn("unrecognized", err)
                self.nothing_sent()

    def test_default_policy_needs_a_higher_limit(self) -> None:
        args = [a for a in self.apply_args() if a not in ("--policy", self.policy_file)]
        code, out, err = self.run_cli(*args, stdin=self.confirmed())
        self.assertEqual(code, 2)
        self.assertIn("needs 13 mailbox WRMSRs", err)
        self.assertIn("allows 1", err)
        self.assertNotIn("CONFIRM", out)
        self.nothing_sent()

    def test_predicted_refusal_is_reported_before_the_prompt(self) -> None:
        path = self.write("bad.toml", profile_text(e_core(0, oc_voltage_mv=1500)))  # G-124: an OC point carries the V target
        args = self.apply_args()
        args[1] = path
        code, out, err = self.run_cli(*args, stdin=self.confirmed())
        self.assertEqual(code, 2)
        self.assertIn("CHECK_FAILED", out)
        self.assertNotIn("CONFIRM", out)
        self.nothing_sent()

    def test_hardware_faults_exit_3(self) -> None:
        cases = {
            "oc locked": sim_config(oc_locked=True),
            "uvp": sim_config(uvp=True),
        }
        for name, config in cases.items():
            with self.subTest(name):
                self.audit.unlink(missing_ok=True)
                code, out, _ = self.run_cli(*self.apply_args(), stdin=self.confirmed(), sim=SimMsrPort(config))
                self.assertEqual(code, 3)
                self.assertIn("STOPPED (rc)", out)
                self.assertEqual(self.sim.vf(4), P4_BASELINE)
        sim = SimMsrPort(sim_config())
        sim.silent_ignore(0x11)
        self.audit.unlink(missing_ok=True)
        code, out, _ = self.run_cli(*self.apply_args(), stdin=self.confirmed(), sim=sim)
        self.assertEqual(code, 3)
        self.assertIn("STOPPED (unverified)", out)
        self.assertIn("guard is tripped", out)
        self.assertIn("trip", [r["event"] for r in read_all(self.audit)])

    def test_a_guard_refusal_during_the_run_exits_2(self) -> None:
        gate = lambda phase: (False, "no")  # noqa: E731
        code, out, _ = self.run_cli(*self.apply_args(), stdin=self.confirmed(), health_gate=gate)
        self.assertEqual(code, 2)
        self.assertIn("HEALTH_GATE", out)
        self.assertEqual(self.sim.writes, [])

    def test_health_gate_is_injectable_and_used(self) -> None:
        phases: list[str] = []

        def gate(phase: str) -> tuple[bool, str]:
            phases.append(phase)
            return True, "clean"

        code, _, _ = self.run_cli(*self.apply_args(), stdin=self.confirmed(), health_gate=gate)
        self.assertEqual(code, 0)
        self.assertEqual(phases, ["before", "after"] * 9)
        self.assertEqual([r.get("health_gate") for r in read_all(self.audit) if r["event"] == "attempt"], [{"before": "clean"}] * 9)

    def test_allow_taint_bit_is_parsed_and_passed_to_the_health_gate_factory(self) -> None:
        seen: list[frozenset[int]] = []

        def factory(bits: frozenset[int]):
            seen.append(bits)
            return lambda phase: (True, f"bits={sorted(bits)}")

        code, _, _ = self.run_cli(*self.apply_args("--allow-taint-bit", 5, "--allow-taint-bit", "3"), stdin=self.confirmed(), health_gate_factory=factory)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [frozenset({3, 5})])
        self.assertIn("bits=[3, 5]", json.dumps(read_all(self.audit)))
        for bad in ("64", "-1", "x"):
            self.assertEqual(self.run_cli(*self.apply_args("--allow-taint-bit", bad), stdin=self.confirmed())[0], 1)

    def test_mode_is_required_and_exclusive(self) -> None:
        self.assertEqual(self.run_cli("apply", self.profile_file)[0], 1)
        self.assertEqual(self.run_cli("apply", self.profile_file, "--sim", "--device")[0], 1)
        self.nothing_sent()

    def test_bad_inputs_exit_1(self) -> None:
        self.assertEqual(self.run_cli("apply", self.tmp / "missing.toml", "--sim", stdin=self.confirmed())[0], 1)
        bad_policy = self.write("badpolicy.toml", "cpus = [0]\nunknown_key = 1\n")
        code, _, err = self.run_cli("apply", self.profile_file, "--sim", "--policy", bad_policy, stdin=self.confirmed())
        self.assertEqual(code, 1)
        self.assertIn("policy error", err)
        self.nothing_sent()

    def test_a_default_audit_path_is_a_state_directory_file(self) -> None:  # G-106: not installed (no /etc/arltuner/policy.toml) => legacy
        self.assertEqual(cli.default_audit_path({"HOME": "/home/x"}, lstat_fn=not_installed), Path("/home/x/.local/state/linuxmon/audit.jsonl"))
        self.assertEqual(cli.default_audit_path({"USERPROFILE": "C:/Users/x"}, lstat_fn=not_installed), Path("C:/Users/x/.local/state/linuxmon/audit.jsonl"))

    def test_a_default_lock_path_is_run_lock_when_it_exists(self) -> None:
        self.assertEqual(cli.default_lock_path(isdir=lambda p: p == "/run/lock", lstat_fn=not_installed), Path("/run/lock/linuxmon-mailbox.lock"))
        fallback = cli.default_lock_path(isdir=lambda p: False, lstat_fn=not_installed)
        self.assertEqual(fallback.name, "linuxmon-mailbox.lock")
        self.assertEqual(fallback.parent, Path(tempfile.gettempdir()))

    def test_every_apply_request_goes_through_the_guard(self) -> None:
        with mock.patch("arltuner.cli.WriteGuard", wraps=WriteGuard) as guard_cls:
            code, _, _ = self.run_cli(*self.apply_args(), stdin=self.confirmed())
        self.assertEqual(code, 0)
        guard_cls.assert_called_once()
        events = [r["event"] for r in read_all(self.audit)]
        self.assertEqual(len(self.sim.requests), events.count("attempt") + events.count("readback"))


class Measured(CliCase):
    """``--sim`` without an injected simulator is the machine as MEASURED on Windows (``SimConfig.measured``) with the zero-field rule on."""

    def run_default_sim(self, *argv: object, **kwargs):
        return self.run_cli(*argv, sim_factory=None, **kwargs)

    def test_plan_for_cpu_1_prints_xtus_word_and_the_step_list(self) -> None:
        path = self.write("p1.toml", profile_text('kind = "P"\nindex = 1\noffset_mv = -5\nratio = 55'))
        code, out, err = self.run_default_sim("plan", path, "--sim", "--policy", self.policy_file)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("Operations required: 11 mailbox WRMSRs = 8 guard operations", out)  # 0x14 reads 0x8 on the measured machine: no 0x15
        self.assertNotIn("cmd 0x15", out)
        self.assertIn("data 0xff600037", out)  # CPU 1 at -5 mV: ratio 55 (0x1C) | offset, no OC point -> V target 0 (G-120)
        self.assertIn("favored-core word of P-Core 1: data 0x37", out)
        self.assertIn("granular-ratio word of P-Core 1: data 0x3", out)
        self.assertIn("ratio 0 -> 55", out)  # the measured baseline word is 0x0
        self.assertIn("effective -54.88 mV", out)

    def test_apply_cpu_0_and_revert_note_on_the_measured_machine(self) -> None:
        path = self.write("p0.toml", profile_text('kind = "P"\nindex = 0\noffset_mv = -5\nratio = 57'))
        args = ["apply", path, "--sim", "--policy", self.policy_file, "--audit", self.audit, "--lock", self.lock]
        code, out, err = self.run_default_sim(*args, stdin=self.confirmed())
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("Result: OK", out)
        self.assertIn("0xff600039", out)  # G-120: P0 ratio 57 = top fused ratio, no OC point, target 0
        self.assertIn("a reboot restores the BIOS-programmed state; a write cannot clear an OC ratio above the top fused ratio", out)
        self.assertIn("(restores the word in effect before the write", out)

    def test_read_vf_of_a_measured_zero_word(self) -> None:
        code, out, err = self.run_default_sim("read-vf", "--cpu", 1, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed())
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("ratio    : 0x", out)
        self.assertIn("raw word : 0x00000000", out)

    def test_read_vf_after_gate_trip_reports_the_word_and_exits_hardware(self) -> None:
        def gate(phase: str):
            return (True, "clean") if phase == "before" else (False, "UNCLEAN: new taint bit(s) 2 (taint 12288 -> 12292)")

        code, out, err = self.run_default_sim("read-vf", "--cpu", 1, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed(), health_gate=gate)
        self.assertEqual(code, 3, out)
        self.assertIn("raw word : 0x00000000", out)
        self.assertIn("new taint bit(s) 2", err + out)


class RaiseThroughTheCli(CliCase):
    """D-016 / T-022: the reviewed policy file is the only thing that can raise anything. ``policies/example-oc-ramp.toml`` is the example
    (E ratio 49 / 1.250 V, P ratio 58, shipped step limits; T-023 / D-017 lowered the E ceiling from 50 to 49); it is read only because a test names it. ``--sim`` without an injected simulator
    is the measured machine (``SimConfig.measured``) with the zero-field rule on."""

    def run_default_sim(self, *argv: object, **kwargs):
        return self.run_cli(*argv, sim_factory=None, **kwargs)

    EXAMPLE = Path(__file__).resolve().parents[1] / "policies" / "example-oc-ramp.toml"
    E_UP = profile_text('kind = "E"\nindex = 0\nratio = 49\noc_voltage_mv = 1176')  # 48 -> 49 and +20 mV on cluster 0
    E_FAR = profile_text('kind = "E"\nindex = 0\nratio = 49\noc_voltage_mv = 1200')  # more than one step (the V target: +44 mV)

    def test_the_default_policy_refuses_a_raise_with_the_reason(self) -> None:
        path = self.write("e_up.toml", self.E_UP)
        code, out, err = self.run_default_sim("plan", path, "--sim")  # no --policy: the built-in policy
        self.assertEqual(code, 2, out + err)
        self.assertIn("no-raise value", out)
        self.assertIn("NOT ALLOWED", out)

    def test_the_example_policy_allows_a_small_raise_voltage_first(self) -> None:
        path = self.write("e_up.toml", self.E_UP)
        code, out, err = self.run_default_sim("plan", path, "--sim", "--policy", self.EXAMPLE)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("ALLOWED", out)
        self.assertIn("[1/2 voltage first]", out)
        self.assertIn("[2/2 full word]", out)
        self.assertIn("read hardware maximum voltage (0x28) of E-Core 0", out)
        self.assertIn("this plan RAISES", out)  # the [UNVERIFIED] caveat
        self.assertNotIn("NEXT ALLOWED STEP", out)

    def test_a_request_beyond_the_step_limits_prints_the_next_step_and_exits_2(self) -> None:
        path = self.write("e_far.toml", self.E_FAR)
        code, out, err = self.run_default_sim("plan", path, "--sim", "--policy", self.EXAMPLE)
        self.assertEqual(code, 2, out + err)
        self.assertIn("NEXT ALLOWED STEP", out)
        self.assertIn("ratio = 49", out)
        self.assertIn("oc_voltage_mv = 1176", out)
        self.assertIn("needs a smaller step", out)
        self.nothing_sent_by_plan(out)

    def nothing_sent_by_plan(self, out: str) -> None:
        self.assertIn("nothing was sent", out)
        self.assertFalse(self.audit.exists())

    def test_apply_with_the_example_policy_runs_the_raise_and_prints_a_frequency_first_revert(self) -> None:
        path = self.write("e_up.toml", self.E_UP)
        args = ["apply", path, "--sim", "--policy", self.EXAMPLE, "--audit", self.audit, "--lock", self.lock]
        code, out, err = self.run_default_sim(*args, stdin=self.confirmed())
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("Result: OK", out)
        self.assertIn("a reboot restores the BIOS-programmed state", out)
        self.assertIn("after a raise the ratio first, then the voltage", out)
        self.assertEqual(len(re.findall(r"cmd 0x11 p1 0 p2 0 data", out)), 8)  # the revert plan: two words per CPU of the cluster
        self.assertIn("this plan RAISES", out)

    def test_apply_refuses_a_big_step_and_prints_the_next_one(self) -> None:
        path = self.write("e_far.toml", self.E_FAR)
        args = ["apply", path, "--sim", "--policy", self.EXAMPLE, "--audit", self.audit, "--lock", self.lock]
        code, out, err = self.run_default_sim(*args, stdin=self.confirmed())
        self.assertEqual(code, 2, out + err)
        self.assertIn("NEXT ALLOWED STEP", out)
        self.assertNotIn("CONFIRM", out)  # refused before the prompt
        self.assertFalse(self.audit.exists())

    def test_a_value_above_the_policy_ceiling_is_refused_before_anything_else(self) -> None:
        path = self.write("e_high.toml", profile_text('kind = "E"\nindex = 0\nratio = 50'))  # the example's E ceiling is 49 (D-017)
        code, out, err = self.run_default_sim("apply", path, "--sim", "--policy", self.EXAMPLE, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed())
        self.assertEqual(code, 2, out + err)
        self.assertIn("ratio_max", err)
        self.assertNotIn("CONFIRM", out)
        self.assertFalse(self.audit.exists())

    def test_the_example_file_is_inert_and_valid(self) -> None:
        from arltuner.guard import default_m1_policy, load_policy

        example = load_policy(self.EXAMPLE)
        shipped = default_m1_policy()
        self.assertEqual(example.allow, shipped.allow)  # the same allowlist: the file only adds numbers
        self.assertEqual(dict(example.ratio_max), {"P": 58, "E": 49})
        self.assertEqual(dict(example.v_target_max_mv), {"P": None, "E": 1250})  # P blank = the fused voltage
        self.assertEqual((example.max_ratio_step, example.max_v_step_mv, example.max_offset_step_mv, example.offset_ceiling_mv), (1, 20, 25, 0))
        self.assertEqual((dict(shipped.ratio_max), dict(shipped.v_target_max_mv)), ({"P": None, "E": None}, {"P": None, "E": None}))
        src = Path(cli.__file__).resolve().parents[1]
        for path in src.rglob("*.py"):
            self.assertNotIn("example-oc-ramp", path.read_text(encoding="utf-8"), path)  # no source file loads it by name
        text = self.EXAMPLE.read_text(encoding="utf-8")
        self.assertNotIn("\r", text)  # LF everywhere
        self.assertIn("NOT LOADED BY DEFAULT", text)
        self.assertIn("THE OPERATOR OWNS THE NUMBERS", text)


class ReadVf(CliCase):
    def test_reads_cpu_4_and_prints_the_word(self) -> None:
        code, out, err = self.run_cli("read-vf", "--sim", "--cpu", 4, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed())
        self.assertEqual((code, err), (0, ""), out)
        for needle in ("ratio    : 55x", "1.316 V", "raw 0x544", "adaptive", "+0.00 mV", "0x00054437", "simulator"):
            self.assertIn(needle, out)
        self.assertEqual(self.sim.requests, [SimRequest(4, 0x10, 0, 0, 0)])  # exactly one mailbox request
        self.assertEqual(len(self.sim.writes), 1)  # a mailbox read is one WRMSR
        events = [r["event"] for r in read_all(self.audit)]
        self.assertEqual(events, ["attempt", "result"])
        self.assertFalse(self.lock.exists())

    def test_sim_is_the_default_mode(self) -> None:
        code, out, _ = self.run_cli("read-vf", "--cpu", 4, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed())
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.sim.requests), 1)

    def test_prints_a_modified_word(self) -> None:
        sim = SimMsrPort(sim_config(initial_vf={5: VfWord(52, 0x500, False, -31)}))
        code, out, _ = self.run_cli("read-vf", "--sim", "--cpu", 5, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed(), sim=sim)
        self.assertEqual(code, 0)
        self.assertIn("52x", out)
        self.assertIn("1.250 V", out)
        self.assertIn("-30.27 mV", out)
        self.assertIn("raw -31", out)

    def test_session_limit_is_forced_to_one_even_with_a_generous_policy(self) -> None:
        with mock.patch("arltuner.cli.WriteGuard", wraps=WriteGuard) as guard_cls:
            code, _, _ = self.run_cli("read-vf", "--sim", "--cpu", 4, "--policy", self.policy_file, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed())
        self.assertEqual(code, 0)
        self.assertEqual(guard_cls.call_args.args[1].max_operations_per_session, 1)

    def read_vf_args(self, *extra: object) -> list[object]:
        return ["read-vf", "--sim", "--cpu", 4, "--audit", self.audit, "--lock", self.lock, *extra]

    def test_a_second_read_vf_soon_after_the_first_is_refused_with_the_wait_time(self) -> None:
        # T-016: one process per command, so the dwell must come from the audit log (D-011).
        t0 = 1_700_000_000.0
        self.assertEqual(self.run_cli(*self.read_vf_args(), stdin=self.confirmed(), wall_clock=lambda: t0)[0], 0)
        code, out, err = self.run_cli(*self.read_vf_args(), stdin=self.confirmed(), wall_clock=lambda: t0 + 10.0)
        self.assertEqual(code, 2, out + err)
        self.assertIn("refused by the guard: DWELL", err)
        self.assertIn("another process", err)
        self.assertIn("DWELL: wait about 50 s before the next mailbox operation", err)  # the CLI's own hint
        self.assertIn("Nothing was sent.", err)
        self.assertEqual(len(self.sim.requests), 1)  # only the first process's request ever reached the transport
        code, out, err = self.run_cli(*self.read_vf_args(), stdin=self.confirmed(), wall_clock=lambda: t0 + 61.0)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(len(self.sim.requests), 2)
        self.assertEqual([r["event"] for r in read_all(self.audit)].count("attempt"), 2)

    def test_the_per_boot_cap_survives_process_boundaries(self) -> None:
        policy = self.write("boot.toml", POLICY_TOML.replace("min_dwell_s = 60.0", "min_dwell_s = 0.0").replace("per_boot = 200", "per_boot = 2"))
        args = self.read_vf_args("--policy", policy)
        for _ in range(2):
            self.assertEqual(self.run_cli(*args, stdin=self.confirmed())[0], 0)
        code, out, err = self.run_cli(*args, stdin=self.confirmed())
        self.assertEqual(code, 2, out + err)
        self.assertIn("refused by the guard: BOOT_LIMIT", err)
        self.assertIn("BOOT_LIMIT: no further mailbox operation is allowed until the next reboot", err)  # the CLI's own hint
        self.assertIn("Nothing was sent.", err)
        self.assertEqual(len(self.sim.requests), 2)
        # another boot starts a new count
        self.assertEqual(self.run_cli(*args, stdin=self.confirmed(), boot_id_fn=lambda: "another-boot")[0], 0)
        self.assertEqual(len(self.sim.requests), 3)

    def test_apply_is_refused_up_front_when_the_boot_budget_cannot_cover_the_whole_plan(self) -> None:
        # the profile needs 13 WRMSRs (9 operations + 4 read-backs); 5 of this boot's 17 are already recorded by "another process"
        policy = self.write("budget.toml", POLICY_TOML.replace("min_dwell_s = 60.0", "min_dwell_s = 0.0").replace("per_boot = 200", "per_boot = 17"))
        used = "".join(f'{{"event":"attempt","ts":"2023-11-14T22:13:2{i}.000+00:00","boot_id":"test-boot"}}\n' for i in range(5))
        self.audit.write_text(used)
        code, out, err = self.run_cli(*self.apply_args("--policy", policy), stdin=self.confirmed())
        self.assertEqual(code, 2, out + err)
        self.assertIn("remain in this boot's budget", err)
        self.assertIn("nothing was sent", err)
        self.nothing_sent()  # refused before the first request: apply can never stop half-way on BOOT_LIMIT
        # the same records belong to another boot -> not counted against this one
        code, out, err = self.run_cli(*self.apply_args("--policy", policy), stdin=self.confirmed(), boot_id_fn=lambda: "a-fresh-boot")
        self.assertEqual(code, 0, out + err)

    def test_an_unreadable_audit_log_refuses_apply_before_anything_is_sent(self) -> None:
        self.audit.write_text('not json\n{"event":"trip"}\n')
        code, out, err = self.run_cli(*self.apply_args(), stdin=self.confirmed())
        self.assertEqual(code, 2, out + err)
        self.assertIn("per-boot budget cannot be checked", err)
        self.nothing_sent()

    def test_an_unreadable_audit_log_is_reported_and_nothing_is_sent(self) -> None:
        self.audit.write_text('{"event":"attempt","ts":"2023-11-14T22:13:20.000+00:00","boot_id":"x"}\nnot json\n{"event":"trip"}\n')
        code, out, err = self.run_cli(*self.read_vf_args(), stdin=self.confirmed())
        self.assertEqual(code, 2, out + err)
        self.assertIn("refused by the guard: AUDIT_UNAVAILABLE", err)
        self.assertIn(f"AUDIT_UNAVAILABLE: the audit log {self.audit} must be readable", err)  # the CLI's own hint
        self.assertIn("Nothing was sent.", err)
        self.nothing_sent()

    def test_the_policy_boot_cap_key_is_passed_through_to_the_guard(self) -> None:
        policy = self.write("cap.toml", POLICY_TOML.replace("per_boot = 200", "per_boot = 7"))
        with mock.patch("arltuner.cli.WriteGuard", wraps=WriteGuard) as guard_cls:
            code, _, _ = self.run_cli(*self.read_vf_args("--policy", policy), stdin=self.confirmed())
        self.assertEqual(code, 0)
        guard_policy = guard_cls.call_args.args[1]
        self.assertEqual((guard_policy.max_operations_per_boot, guard_policy.max_operations_per_session), (7, 1))

    def test_cpu_outside_0_to_23_is_refused(self) -> None:
        for cpu in (24, 100, -1):
            with self.subTest(cpu=cpu):
                code, out, err = self.run_cli("read-vf", "--sim", "--cpu", cpu, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed())
                self.assertEqual(code, 2)
                self.assertIn("not a CPU of this system", err)
                self.assertNotIn("CONFIRM", out)
                self.nothing_sent()
        self.assertEqual(self.run_cli("read-vf", "--sim", "--cpu", "x")[0], 1)
        self.assertEqual(self.run_cli("read-vf", "--sim")[0], 1)  # --cpu is required

    def test_env_flag_and_confirmation_are_required(self) -> None:
        code, out, err = self.run_cli("read-vf", "--sim", "--cpu", 4, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed(), env={})
        self.assertEqual(code, 2)
        self.assertIn(ENV_FLAG, err)
        self.assertNotIn("CONFIRM", out)
        self.nothing_sent()
        code, _, err = self.run_cli("read-vf", "--sim", "--cpu", 4, "--audit", self.audit, "--lock", self.lock, stdin="CONFIRM wrong\n")
        self.assertEqual(code, 2)
        self.assertIn("confirmation does not match", err)
        self.nothing_sent()

    def test_a_policy_that_does_not_allow_the_read_is_refused_before_the_prompt(self) -> None:
        only_low_cpus = self.write("low.toml", POLICY_TOML.replace('cpus = ["0-23"]', "cpus = [0, 1]"))
        code, out, err = self.run_cli("read-vf", "--sim", "--cpu", 4, "--policy", only_low_cpus, stdin=self.confirmed())
        self.assertEqual(code, 2)
        self.assertIn("CPU_NOT_ALLOWED", err)
        self.assertNotIn("CONFIRM", out)
        no_read = self.write("noread.toml", POLICY_TOML.replace("cmd = 0x10", "cmd = 0x1E"))  # 0x1E: a named command no entry shares
        code, _, err = self.run_cli("read-vf", "--sim", "--cpu", 4, "--policy", no_read, stdin=self.confirmed())
        self.assertEqual(code, 2)
        self.assertIn("NOT_ALLOWLISTED", err)
        self.nothing_sent()

    def test_a_nonzero_completion_code_exits_3(self) -> None:
        sim = SimMsrPort(sim_config())
        sim.force_rc(0x10, 5)
        code, _, err = self.run_cli("read-vf", "--sim", "--cpu", 4, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed(), sim=sim)
        self.assertEqual(code, 3)
        self.assertIn("OCNotSupported", err)

    def test_a_transport_timeout_exits_3(self) -> None:
        sim = SimMsrPort(sim_config())
        sim.stuck_busy(4, before_write=True)
        code, _, err = self.run_cli("read-vf", "--sim", "--cpu", 4, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed(), sim=sim)
        self.assertEqual(code, 3)
        self.assertIn("MailboxTimeout", err)
        self.assertIn("request written: False", err)
        self.assertEqual(sim.writes, [])


class ReadGeneric(CliCase):
    """``read``: ONE guarded read of any ``kind = "read"`` allow entry (T-024)."""

    def run_default_sim(self, *argv: object, **kwargs):
        return self.run_cli(*argv, sim_factory=None, **kwargs)

    def read_args(self, cpu: object, cmd: object, *extra: object) -> list[object]:
        return ["read", "--sim", "--cpu", cpu, "--cmd", cmd, "--policy", self.policy_file, "--audit", self.audit, "--lock", self.lock, *extra]

    def test_favored_word_of_an_e_core_on_the_measured_machine(self) -> None:
        code, out, err = self.run_default_sim(*self.read_args(8, "0x1C"), stdin=self.confirmed())
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("favored ratio [7:0]  : 48x", out)  # 0x30
        self.assertIn("fused/limit [15:8]   : 46", out)  # 0x2E
        self.assertIn("raw word : 0x00002e30", out)
        self.assertEqual([r["event"] for r in read_all(self.audit)], ["attempt", "result"])

    def test_decimal_cmd_granular_and_per_core_enable_decodes(self) -> None:
        code, out, err = self.run_default_sim(*self.read_args(1, 38), stdin=self.confirmed())
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("granular index [2:0] : 3", out)
        self.assertIn("raw word : 0x00000003", out)
        code, out, err = self.run_default_sim(*self.read_args(1, "0x14", "--audit", self.tmp / "a2.jsonl"), stdin=self.confirmed())
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("per-core enable [3]  : 1", out)  # 0x8

    def test_cmd_0x10_prints_the_vf_decode(self) -> None:
        code, out, err = self.run_cli(*self.read_args(4, "0x10"), stdin=self.confirmed())
        self.assertEqual((code, err), (0, ""), out)
        for needle in ("ratio    : 55x", "1.316 V", "adaptive", "raw word : 0x00054437"):
            self.assertIn(needle, out)
        self.assertEqual(self.sim.requests, [SimRequest(4, 0x10, 0, 0, 0)])

    def test_p1_is_part_of_the_shape(self) -> None:
        code, out, err = self.run_default_sim(*self.read_args(8, "0x09", "--p1", "0x11"), stdin=self.confirmed())
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("P1=0x11", out)
        code, _, err = self.run_cli(*self.read_args(8, "0x09", "--p1", "0x12"), stdin=self.confirmed())
        self.assertEqual(code, 2)
        self.assertIn("Nothing was sent", err)

    def test_a_cmd_no_entry_matches_is_refused_before_the_prompt(self) -> None:
        code, out, err = self.run_cli(*self.read_args(4, "0x30"), stdin="")
        self.assertEqual(code, 2, out)
        self.assertIn("no allow entry", err)
        self.assertIn("Nothing was sent", err)
        self.assertNotIn("CONFIRM", out + err)
        self.nothing_sent()
        self.assertFalse(self.audit.exists())

    def test_a_write_cmd_is_refused_even_when_the_policy_allowlists_it(self) -> None:
        self.assertIn("vf_write", POLICY_TOML)
        code, out, err = self.run_cli(*self.read_args(4, "0x11"), stdin=self.confirmed())
        self.assertEqual(code, 2, out)
        self.assertIn("kind 'write'", err)
        self.assertIn("Nothing was sent", err)
        self.assertNotIn("CONFIRM", out + err)
        self.nothing_sent()

    def test_a_cpu_outside_the_policy_is_refused_by_predict(self) -> None:
        only_low = self.write("low.toml", POLICY_TOML.replace('cpus = ["0-23"]', "cpus = [0, 1]"))
        code, out, err = self.run_cli("read", "--sim", "--cpu", 8, "--cmd", "0x1C", "--policy", only_low, stdin=self.confirmed())
        self.assertEqual(code, 2)
        self.assertIn("CPU_NOT_ALLOWED", err)
        self.assertNotIn("CONFIRM", out)
        self.assertIn("Nothing was sent", err)
        self.nothing_sent()

    def test_env_flag_missing_is_refused(self) -> None:
        code, out, err = self.run_cli(*self.read_args(8, "0x1C"), stdin=self.confirmed(), env={})
        self.assertEqual(code, 2, out)
        self.assertIn(ENV_FLAG, err)
        self.nothing_sent()

    def test_wrong_confirmation_is_refused(self) -> None:
        code, _, err = self.run_cli(*self.read_args(8, "0x1C"), stdin="CONFIRM wrong\n")
        self.assertEqual(code, 2)
        self.nothing_sent()

    def test_after_gate_trip_prints_the_word_then_exits_hardware(self) -> None:
        def gate(phase: str):
            return (True, "clean") if phase == "before" else (False, "UNCLEAN: new taint bit(s) 2 (taint 12288 -> 12292)")

        code, out, err = self.run_default_sim(*self.read_args(8, "0x1C"), stdin=self.confirmed(), health_gate=gate)
        self.assertEqual(code, 3, out)
        self.assertIn("raw word : 0x00002e30", out)
        self.assertIn("new taint bit(s) 2", err + out)

    def test_bad_numbers_are_usage_errors(self) -> None:
        self.assertEqual(self.run_cli("read", "--cpu", 1, "--cmd", "zz")[0], 1)
        self.assertEqual(self.run_cli("read", "--cpu", 1, "--cmd", "0x100")[0], 1)
        self.assertEqual(self.run_cli("read", "--cpu", 1)[0], 1)  # --cmd is required


class DeviceRefusals(CliCase):
    """``--device``: every refusal happens before the port is constructed. ``DevCpuMsrPort`` is replaced by a mock that must
    never be called; ``device_port_factory`` stays at its default so that a leak would reach the mock, not ``/dev/cpu``."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch("arltuner.cli.DevCpuMsrPort")
        self.dev = patcher.start()
        self.addCleanup(patcher.stop)

    def refused(self, *argv: object, code: int = 2, **kwargs):
        got, out, err = self.run_cli(*argv, **kwargs)
        self.assertEqual(got, code, (out, err))
        self.dev.assert_not_called()
        self.assertEqual(self.dev.mock_calls, [])
        self.nothing_sent()
        return out, err

    def device_apply(self, *extra: object) -> list[object]:
        return ["apply", self.profile_file, "--device", "--policy", self.policy_file, "--audit", self.audit, "--lock", self.lock, *extra]

    def device_read(self, cpu: object = 4, *extra: object) -> list[object]:
        return ["read-vf", "--device", "--cpu", cpu, "--audit", self.audit, "--lock", self.lock, *extra]

    def test_apply_env_flag_absent(self) -> None:
        _, err = self.refused(*self.device_apply(), stdin=self.confirmed(), env={})
        self.assertIn(ENV_FLAG, err)

    def test_apply_wrong_confirmation(self) -> None:
        out, err = self.refused(*self.device_apply(), stdin="CONFIRM wrong\n")
        self.assertIn("confirmation does not match", err)
        self.assertIn("baseline words are read through the guard after confirmation", out)  # a preview, not an exact plan

    def test_apply_no_confirmation_at_all(self) -> None:
        self.refused(*self.device_apply(), stdin="")

    def test_apply_needs_a_higher_limit_with_the_default_policy(self) -> None:
        args = [a for a in self.device_apply() if a not in ("--policy", self.policy_file)]
        _, err = self.refused(*args, stdin=self.confirmed())
        self.assertIn("needs 13 mailbox WRMSRs", err)

    def test_apply_policy_refusal(self) -> None:
        low = self.write("low.toml", POLICY_TOML.replace('cpus = ["0-23"]', "cpus = [0, 1]"))
        args = ["apply", self.profile_file, "--device", "--policy", low, "--audit", self.audit, "--lock", self.lock]
        _, err = self.refused(*args, stdin=self.confirmed())
        self.assertIn("CPU_NOT_ALLOWED", err)

    def test_apply_topology_mismatch(self) -> None:
        def mismatch():
            raise TopologyMismatch("cpu_core/cpus is [0, 1] but the 285K map expects [0..7]")

        _, err = self.refused(*self.device_apply(), stdin=self.confirmed(), detect_fn=mismatch)
        self.assertIn("could not be derived", err)

    def test_the_real_sysfs_is_not_read_when_detect_is_injected(self) -> None:
        calls = []

        def detect():
            calls.append(1)
            return arl_s_285k()

        self.refused(*self.device_apply(), stdin="CONFIRM wrong\n", detect_fn=detect)
        self.assertEqual(calls, [1])

    def test_device_without_a_health_gate_is_refused_before_the_port_exists(self) -> None:
        for argv in (self.device_apply(), self.device_read()):
            with self.subTest(argv=argv[0]):
                _, err = self.refused(*argv, stdin=self.confirmed(), device_health_gate_factory=lambda bits: None)
                self.assertIn("no health gate is available", err)

    def test_allow_taint_bits_reach_the_device_health_gate_factory(self) -> None:
        seen: list[frozenset[int]] = []

        def factory(bits: frozenset[int]):
            seen.append(bits)
            return None  # refuse: we only want to observe what the factory was given

        self.refused(*self.device_apply("--allow-taint-bit", 5, "--allow-taint-bit", 3), stdin=self.confirmed(), device_health_gate_factory=factory)
        self.assertEqual(seen, [frozenset({3, 5})])

    def test_default_device_health_gate_factory_builds_the_real_bracket_over_root(self) -> None:
        from pathlib import Path

        from arltuner.cli import _default_device_health_gate_factory

        with mock.patch("arltuner.health.make_health_gate", return_value="GATE") as make:
            self.assertEqual(_default_device_health_gate_factory(frozenset({2})), "GATE")
        make.assert_called_once_with(Path("/"), allowed_new_taint_bits=frozenset({2}))

    def test_read_vf_cpu_out_of_range(self) -> None:
        for cpu in (24, -1):
            with self.subTest(cpu=cpu):
                _, err = self.refused(*self.device_read(cpu), stdin=self.confirmed())
                self.assertIn("not a CPU of this system", err)

    def test_read_vf_env_flag_absent(self) -> None:
        self.refused(*self.device_read(), stdin=self.confirmed(), env={})

    def test_read_vf_wrong_confirmation(self) -> None:
        self.refused(*self.device_read(), stdin="CONFIRM wrong\n")

    def test_read_vf_policy_refusal(self) -> None:
        low = self.write("low.toml", POLICY_TOML.replace('cpus = ["0-23"]', "cpus = [0, 1]"))
        _, err = self.refused(*self.device_read(4, "--policy", low), stdin=self.confirmed())
        self.assertIn("CPU_NOT_ALLOWED", err)

    def test_read_vf_topology_mismatch(self) -> None:
        def mismatch():
            raise TopologyMismatch("not an Arrow Lake-S")

        self.refused(*self.device_read(), stdin=self.confirmed(), detect_fn=mismatch)

    def test_mode_flags_are_exclusive(self) -> None:
        self.refused("read-vf", "--device", "--sim", "--cpu", 4, code=1)

    def test_usage_errors_do_not_build_a_port(self) -> None:
        self.refused("read-vf", "--device", code=1)  # --cpu missing
        self.refused("apply", "--device", code=1)  # profile missing


class Usage(CliCase):
    def test_no_command_is_a_usage_error(self) -> None:
        code, _, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("usage", err.lower())

    def test_unknown_command_is_a_usage_error(self) -> None:
        self.assertEqual(self.run_cli("frobnicate")[0], 1)

    def test_help_exits_0_on_stdout(self) -> None:
        code, out, err = self.run_cli("--help")
        self.assertEqual((code, err), (0, ""))
        for word in ("topology", "plan", "apply", "read-vf", "read"):
            self.assertIn(word, out)
        code, out, _ = self.run_cli("read-vf", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--cpu", out)

    def test_default_device_factory_builds_a_writable_dev_cpu_port(self) -> None:
        with mock.patch("arltuner.cli.DevCpuMsrPort") as dev:  # never the real class
            port = cli._default_device_port()
        dev.assert_called_once_with(writable=True)
        self.assertIs(port, dev.return_value)

    def test_the_unicode_ratio_caveat_does_not_break_a_legacy_stream(self) -> None:
        # __main__ reconfigures real streams with errors="backslashreplace"; emulate a console that cannot encode the em dash
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict", newline="\n")
        from arltuner.__main__ import _tolerant

        _tolerant(stream)
        stream.write("a line with an em dash \u2014 that a legacy console cannot encode\n")
        stream.flush()
        self.assertIn(b"\\u2014", raw.getvalue())


class PointOffsetsThroughTheCli(CliCase):
    """T-025: ``[[point]]`` profiles through ``plan`` / ``apply``. ``--sim`` without an injected simulator is the measured machine
    (``SimConfig.measured``: ``0x10 P2=6`` of an E CPU = 0x2E, point 7 -> rc 0x11) with V/F points on."""

    POINT_POLICY = POLICY_TOML + """
[[allow]]
name = "vf_point_read"
cmd = 0x10
p1 = [0]
p2 = [1, 2, 3, 4, 5, 6, 7, 8]
kind = "read"

[[allow]]
name = "vf_point_write"
cmd = 0x11
p1 = [0]
p2 = [1, 2, 3, 4, 5, 6, 7, 8]
kind = "write"
check = "vf_point_offset_bounded"
"""
    PROFILE = profile_text() + '\n[[point]]\nkind = "E"\nindex = 0\npoint = 6\noffset_mv = -20\nexpect_ratio = 46\n'

    def run_default_sim(self, *argv: object, **kwargs):
        return self.run_cli(*argv, sim_factory=None, **kwargs)

    def test_the_shipped_policy_refuses_a_point_profile_and_sends_nothing(self) -> None:
        path = self.write("pt.toml", self.PROFILE)
        code, out, err = self.run_default_sim("plan", path, "--sim")
        self.assertEqual(code, 2, out + err)
        self.assertIn("NOT_ALLOWLISTED", out)
        code, out, err = self.run_default_sim("apply", path, "--sim", "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed())
        self.assertEqual(code, 2, out + err)
        self.assertIn("refused:", out + err)  # the shipped session limit (1) cannot even hold the reads; the allowlist is the second wall (plan above)
        self.assertFalse(self.audit.exists() and self.audit.read_text(encoding="utf-8").strip(), "nothing may reach the guard")

    def test_plan_shows_point_ratio_and_offset_before_and_after_and_the_monotonicity_warning(self) -> None:
        path = self.write("pt.toml", self.PROFILE)
        policy = self.write("pt-policy.toml", self.POINT_POLICY)
        code, out, err = self.run_default_sim("plan", path, "--sim", "--policy", policy)
        self.assertEqual((code, err), (0, ""), out)
        for needle in ("point 6 offset -20 mV (expects ratio 46)", "ratio 46 (kept)", "offset +0.00 -> -19.53 mV (raw 0 -> -20)", "cmd 0x11",
                       "V(n) >= V(n-1)", "ALLOWED", "Operations required: 13 mailbox WRMSRs", "(4 CPU(s))"):
            self.assertIn(needle, out)
        self.assertNotIn("cmd 0x1D", out)
        self.assertNotIn("cmd 0x27", out)
        self.assertEqual(out.count("write V/F point 6 offset of E-Core"), 4)

    def test_apply_writes_the_points_and_prints_the_exact_revert(self) -> None:
        path = self.write("pt.toml", self.PROFILE)
        policy = self.write("pt-policy.toml", self.POINT_POLICY)
        code, out, err = self.run_default_sim("apply", path, "--sim", "--policy", policy, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed())
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("Result: OK", out)
        self.assertEqual(out.count("read-back equal"), 4)
        self.assertIn("p2 6 data 0x0000002e (restores the V/F point 6 word as read", out)

    def test_an_expect_ratio_mismatch_is_refused_with_exit_2_and_no_write(self) -> None:
        path = self.write("pt.toml", self.PROFILE.replace("expect_ratio = 46", "expect_ratio = 45"))
        policy = self.write("pt-policy.toml", self.POINT_POLICY)
        code, out, err = self.run_default_sim("plan", path, "--sim", "--policy", policy)
        self.assertEqual(code, 2, out + err)
        self.assertIn("expect_ratio", out)
        code, out, err = self.run_default_sim("apply", path, "--sim", "--policy", policy, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed())
        self.assertEqual(code, 2, out + err)
        self.assertNotIn("Result: OK", out)


if __name__ == "__main__":
    unittest.main()


POINT_POLICY_TOML = """\
cpus = ["0-23"]
max_operations_per_session = 60
max_operations_per_boot = 200
min_dwell_s = 0.0
max_offset_step_mv = 20
assumed_bios_offset_mv = { P = 0, E = 0 }

[[allow]]
name = "pt_read"
cmd = 0x10
p1 = [0, 2]
p2 = [1, 2, 3, 4, 5, 6, 7, 8]
kind = "read"

[[allow]]
name = "pt_write"
cmd = 0x11
p1 = [%s]
p2 = [1, 2, 3, 4, 5, 6, 7, 8]
kind = "write"
check = "vf_point_offset_bounded"
"""


class PointOffset(CliCase):
    """T-028: ``point-offset`` on the ring (P1 2) and a core point against the measured simulator (no hardware)."""

    def setUp(self) -> None:
        super().setUp()
        self.ring_policy = self.write("ring.toml", POINT_POLICY_TOML % "0, 2")
        self.core_only_policy = self.write("core.toml", POINT_POLICY_TOML % "0")
        self.step_policy = self.write("step.toml", POINT_POLICY_TOML.replace("max_offset_step_mv = 20", "max_offset_step_mv = 2") % "0, 2")
        self.sim = SimMsrPort(SimConfig.measured(per_point=True))

    def po(self, *extra: object, policy=None, stdin: str | None = None, **kw):
        args = ["point-offset", "--cpu", 0, "--p1", 2, "--p2", 8, "--sim", "--policy", policy or self.ring_policy,
                "--audit", self.audit, "--lock", self.lock, *extra]
        return self.run_cli(*args, stdin=self.confirmed() if stdin is None else stdin, **kw)

    def test_ring_point_8_happy_path(self) -> None:
        code, out, err = self.po("--offset-mv", -80, "--expect-ratio", 39)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("before : 0xf6600027", out)  # [MEASURED lnx 2026-10-02] ring point 8: 39x, -75.2 mV
        self.assertIn("verified: True", out)
        self.assertIn("revert : cmd 0x11 P1=2 P2=8 data 0xf6600027", out)
        word = self.sim.vf(0, 2, 8)
        self.assertEqual((word.ratio, word.offset_raw), (39, round(-80 * 1024 / 1000)))
        self.assertEqual([(r.cmd, r.p1, r.p2) for r in self.sim.requests if r.cmd in (0x10, 0x11)], [(0x10, 2, 8), (0x11, 2, 8), (0x10, 2, 8)])

    def test_expect_ratio_mismatch_refuses_before_any_write(self) -> None:
        code, out, err = self.po("--offset-mv", -80, "--expect-ratio", 38)
        self.assertEqual(code, 2, out)
        self.assertIn("not the expected 38x", err)
        self.assertEqual([r.cmd for r in self.sim.requests if r.cmd == 0x11], [])

    def test_without_a_p1_2_entry_nothing_is_sent(self) -> None:
        code, out, err = self.po("--offset-mv", -80, policy=self.core_only_policy)
        self.assertEqual(code, 2, out)
        self.assertIn("default deny", err)
        self.nothing_sent()

    def test_p1_7_is_a_usage_error(self) -> None:
        code, out, err = self.run_cli("point-offset", "--cpu", 0, "--p1", 7, "--p2", 8, "--offset-mv", -80, "--sim", "--policy", self.ring_policy)
        self.assertEqual(code, 1)
        self.nothing_sent()

    def test_step_limit_refuses_the_write_and_exits_2(self) -> None:
        code, out, err = self.po("--offset-mv", -90, policy=self.step_policy)
        self.assertEqual(code, 2, out)
        self.assertIn("step", err)
        self.assertEqual(self.sim.vf(0, 2, 8).offset_raw, -77)

    def test_env_flag_is_required(self) -> None:
        code, out, err = self.po("--offset-mv", -80, env={"USER": "tester"})
        self.assertEqual(code, 2, out)
        self.nothing_sent()

    def test_wrong_token_sends_nothing(self) -> None:
        code, out, err = self.po("--offset-mv", -80, stdin="CONFIRM nope\n")
        self.assertEqual(code, 2, out)
        self.nothing_sent()

    def test_sim_ring_table_ends_with_rc_0x11(self) -> None:
        sim = SimMsrPort(SimConfig.measured(per_point=True))
        self.assertEqual([sim.vf(0, 2, n).ratio for n in range(1, 9)], [8, 14, 20, 28, 34, 37, 38, 39])
        self.assertEqual(sim._cmd_vf_read(0, 2, 9, 0)[0], 0x11)  # past ring point 8
        self.assertEqual(sim._cmd_vf_read(0, 2, 8, 0), (0, 0xF6600027))
