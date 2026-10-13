"""G-147 CLI tests: ``apply --neutralize-residual`` (the restore scripts' apply), ``compare-accepted`` (their verdict) and the cap following of ``apply --device``.

Simulator and temp files only; the one ``--device`` test injects the simulator as the device port and a fake cpufreq I/O (it never touches ``/dev/cpu`` or the real sysfs).
"""
from __future__ import annotations

import dataclasses
import json

from arltuner.controls import residual
from arltuner.mailbox import VfWord
from arltuner.msr import SimConfig, SimMsrPort
from arltuner.topology import arl_s_285k

from .controls._support import e_core, p4, point_entry, point_text, profile_text
from .test_cli import POLICY_TOML, TOKEN, CliCase, not_installed

POINT_ENTRIES = """
[[allow]]
name = "vf_point_read"
cmd = 0x10
p1 = [0]
p2 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
kind = "read"

[[allow]]
name = "vf_point_write"
cmd = 0x11
p1 = [0]
p2 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
kind = "write"
check = "vf_point_offset_bounded"
"""
ROOMY = POLICY_TOML.replace("max_operations_per_session = 60", "max_operations_per_session = 600").replace("max_operations_per_boot = 200", "max_operations_per_boot = 2000").replace(
    "min_dwell_s = 60.0", "min_dwell_s = 0.0"
) + POINT_ENTRIES
EC1_WORD = VfWord(48, 0x48C, False, 0).to_u32()
POINT6_OFFSET_RAW = VfWord.mv_to_offset_raw(-25)


def warm_sim() -> SimMsrPort:
    cfg = SimConfig.measured(per_point=True, reject_zero_fields=True)
    initial = dict(cfg.initial_vf)
    initial.update({c: VfWord.from_u32(EC1_WORD) for c in (12, 13, 14, 15)})
    return SimMsrPort(dataclasses.replace(cfg, initial_vf=initial))


class ApplyNeutralize(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.policy_file = self.write("roomy.toml", ROOMY)
        self.accepted = self.write("accepted.toml", point_text(point_entry("E", 4, 6, -25, 46), name="accepted"))

    def apply(self, *extra, sim=None):
        argv = ["apply", self.accepted, "--sim", "--policy", self.policy_file, "--audit", self.audit, "--lock", self.lock, *extra]
        return self.run_cli(*argv, stdin=self.confirmed(), sim=sim if sim is not None else warm_sim())

    def test_the_flag_neutralises_a_residual_oc_point_and_says_so(self) -> None:
        code, out, err = self.apply("--neutralize-residual")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("residual OC point 48x", out)
        w = self.sim.vf(12)
        self.assertEqual((w.ratio, w.offset_raw), (48, 0))
        self.assertLess(w.v_target_raw, 0x48C + POINT6_OFFSET_RAW)
        self.assertNotIn(0x1D, [r.cmd for r in self.sim.requests])
        self.assertTrue([r for r in self.sim.requests if r.cmd == 0x11 and r.p2 == 0 and r.cpu == 12])

    def test_without_the_flag_nothing_changes_for_the_unit(self) -> None:
        code, out, err = self.apply()
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.sim.vf(12).to_u32(), EC1_WORD)

    def test_accepted_before_counts_the_earlier_profiles_oc_ratio_as_accepted(self) -> None:
        earlier = self.write("earlier.toml", profile_text(e_core(4, ratio=48, oc_voltage_mv=1130), name="earlier"))
        code, out, err = self.apply("--neutralize-residual", "--accepted-before", earlier)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.sim.vf(12).to_u32(), EC1_WORD)  # EC1 has an accepted OC ratio: not a residual

    def test_accepted_before_needs_the_flag(self) -> None:
        code, _, err = self.apply("--accepted-before", self.accepted)
        self.assertEqual(code, 1)
        self.assertIn("--neutralize-residual", err)

    def test_the_scan_reads_are_budgeted_before_anything_is_sent(self) -> None:
        tight = self.write("tight.toml", ROOMY.replace("max_operations_per_session = 600", "max_operations_per_session = 30"))
        code, _, err = self.run_cli(
            "apply", self.accepted, "--sim", "--policy", tight, "--audit", self.audit, "--lock", self.lock, "--neutralize-residual", stdin=self.confirmed(), sim=warm_sim()
        )
        self.assertEqual(code, 2)
        self.assertEqual(self.sim.requests, [])


class FakeCpufreq:
    """``vfdump.MeasureIO`` subset used for the caps: ``cpuinfo_max_freq`` moves with a 0x1D, ``scaling_max_freq`` does not."""

    def __init__(self, sim: SimMsrPort) -> None:
        self.max = {c.cpu: sim.cpuinfo_max_khz(c.cpu) for c in arl_s_285k().cores}
        self.cap = dict(self.max)
        self.writes: list[tuple[int, int]] = []
        sim.on_favored_write = lambda cpu, khz: self.max.__setitem__(cpu, khz)

    def read_cap(self, cpu: int) -> int:
        return self.cap[cpu]

    def write_cap(self, cpu: int, khz: int) -> None:
        self.writes.append((cpu, khz))
        self.cap[cpu] = khz

    def read_max_khz(self, cpu: int) -> int:
        return self.max[cpu]


class DeviceCaps(CliCase):
    """``apply --device``: ``scaling_max_freq`` follows ``cpuinfo_max_freq`` after a 0x1D (lowering here: no raise needs a policy ceiling)."""

    def setUp(self) -> None:
        super().setUp()
        self.policy_file = self.write("roomy.toml", ROOMY)
        self.profile_file = self.write("p4.toml", profile_text(p4(offset_mv=-30, ratio=50)))  # P4 55x -> 50x: a deliberate lowering, 0x1D = 50

    def run_device(self, io: FakeCpufreq, sim: SimMsrPort):
        return self.run_cli(
            "apply", self.profile_file, "--device", "--policy", self.policy_file, "--audit", self.audit, "--lock", self.lock,
            stdin=self.confirmed(), sim=sim, device_port_factory=lambda: sim, health_gate=lambda _phase: (True, "ok"),
            measure_io_factory=lambda: io, lstat_fn=not_installed, sleep=lambda _s: None, clock=lambda: 1000.0,
        )

    def test_the_cap_that_sat_at_the_old_maximum_follows_the_new_one(self) -> None:
        sim = SimMsrPort(self.sim.config)
        io = FakeCpufreq(sim)
        code, out, err = self.run_device(io, sim)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(io.max[4], 5_000_000)  # P-core: ratio x 100 MHz
        self.assertEqual(sim.favored_word(4) & 0xFF, 50)
        self.assertEqual(io.cap[4], 5_000_000)  # was 5.5 GHz = the old maximum: lowered with it
        self.assertIn("cpufreq: cpu 4 scaling_max_freq 5500000 -> 5000000", out)
        self.assertEqual(io.writes, [(4, 5_000_000)])  # nothing else was written

    def test_a_cap_the_user_set_lower_is_never_raised(self) -> None:
        sim = SimMsrPort(self.sim.config)
        io = FakeCpufreq(sim)
        io.cap[4] = 4_000_000
        code, out, err = self.run_device(io, sim)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(io.cap[4], 4_000_000)
        self.assertEqual(io.writes, [])


class CompareAccepted(CliCase):
    FUSED_E = 0x48C2E
    WANT = f"12  0 8 F 17 1E 28 {VfWord(46, 0, False, POINT6_OFFSET_RAW).to_u32():X}\n0  39 8\n"

    def files(self, e_words: list[int], p0_words: list[int], fingerprint: bool = True):
        doc = {
            "units": [
                {"cpus": [{"cpu": 12, "oc": {"word": f"{e_words[0]:#010x}"}, "points": [{"word": f"{w:#010x}"} for w in e_words[1:]]}]},
                {"cpus": [{"cpu": 0, "oc": {"word": f"{p0_words[0]:#010x}"}, "points": [{"word": f"{w:#010x}"} for w in p0_words[1:]]}]},
            ]
        }
        self.write("readback.json", json.dumps(doc))
        self.write("EXPECTED-words.txt", "# comment\n" + self.WANT)
        fp = {"schema": 1, "recorded": "x", "cpu": None, "cpus": [{"cpu": c, "kind": "E" if c >= 8 else "P", "index": c, "cluster": 1 if c >= 8 else None, "favored": False, "fused_word": hex(self.FUSED_E if c >= 8 else 0x5B439)} for c in (0, 12)]}
        self.write("fingerprint.json", json.dumps(fp))
        argv = ["compare-accepted", "--readback", self.tmp / "readback.json", "--expected", self.tmp / "EXPECTED-words.txt"]
        return [*argv, "--fingerprint", self.tmp / "fingerprint.json"] if fingerprint else argv

    POINTS = [0x8, 0xF, 0x17, 0x1E, 0x28, VfWord(46, 0, False, POINT6_OFFSET_RAW).to_u32()]

    def test_identical_words_are_ok(self) -> None:
        code, out, err = self.run_cli(*self.files([0, *self.POINTS], [0x39, 0x8]))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("RESTORED: all 2 cores/clusters match the accepted state.", out)
        self.assertNotIn("EQUIVALENT", out)

    def test_a_neutralised_residual_oc_point_is_equivalent_and_the_exit_code_is_success(self) -> None:
        neutral = VfWord(48, 0x466, False, 0).to_u32()
        code, out, err = self.run_cli(*self.files([neutral, *self.POINTS, 0x30], [0x39, 0x8]))
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("cpu12 EQUIVALENT", out)
        self.assertIn(residual.RESIDUAL_NOTE, out)
        self.assertIn("RESTORED: all 2 cores/clusters match the accepted state (1 equivalent: ", out)

    def test_the_unneutralised_residual_is_bad(self) -> None:
        code, out, _ = self.run_cli(*self.files([EC1_WORD, *self.POINTS, 0x30], [0x39, 0x8]))
        self.assertEqual(code, 2)
        self.assertIn("cpu12 BAD", out)
        self.assertIn("STOP: 1 core(s)/cluster(s) differ", out)

    def test_without_the_fingerprint_only_identical_words_match(self) -> None:
        neutral = VfWord(48, 0x466, False, 0).to_u32()
        code, out, _ = self.run_cli(*self.files([neutral, *self.POINTS, 0x30], [0x39, 0x8], fingerprint=False))
        self.assertEqual(code, 2)
        self.assertIn("cpu12 BAD", out)

    def test_p0_xtu_shaped_word_is_equivalent_to_the_cold_word(self) -> None:
        code, out, _ = self.run_cli(*self.files([0, *self.POINTS], [0x5B439, 0x8]))
        self.assertEqual(code, 0, out)
        self.assertIn("cpu0  EQUIVALENT", out)

    def test_a_missing_cpu_is_bad_and_unreadable_input_is_a_usage_error(self) -> None:
        argv = self.files([0, *self.POINTS], [0x39, 0x8])
        self.write("EXPECTED-words.txt", self.WANT + "5  0 8\n")
        code, out, _ = self.run_cli(*argv)
        self.assertEqual(code, 2)
        self.assertIn("cpu5  BAD <missing>", out)
        code, _, err = self.run_cli("compare-accepted", "--readback", self.tmp / "nope.json", "--expected", self.tmp / "EXPECTED-words.txt")
        self.assertEqual(code, 1)
        self.assertIn("cannot read the inputs", err)

    def test_the_command_sends_nothing(self) -> None:
        self.run_cli(*self.files([0, *self.POINTS], [0x39, 0x8]))
        self.nothing_sent()
