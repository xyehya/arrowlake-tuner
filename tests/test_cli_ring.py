"""G-154 (D-045) CLI tests: the ring (cache, mailbox domain P1 = 2) in ``vf-dump`` (JSON + text), ``compare-accepted`` (the restore scripts' verdict) and ``apply`` of an accepted
profile that has a ``[ring]``. Simulator and temp files only; the live words of the reference machine (2026-10-15): ring OC word ``0x28``, points 8..39x and ``P2 = 9`` = 40x,
accepted ring points 8 and 9 = ``0xFCC00027`` / ``0xFCC00028``.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from arltuner.mailbox import VfWord
from arltuner.msr import SimConfig, SimMsrPort
from arltuner.profiles import load_profile
from arltuner.profiles.complete import _ring_of
from arltuner.profiles.expected import format_ring_line
from arltuner.profiles.serialize import dump_profile

from .test_cli import CliCase

REPO = Path(__file__).resolve().parents[1]
SHIPPED = (REPO / "policies/default.toml").read_text()
EXAMPLE = REPO / "examples/reference-285k/profiles"
RING_ACCEPTED = [0x28, 0x08, 0x0E, 0x14, 0x1C, 0x22, 0x25, 0x26, 0xFCC00027, 0xFCC00028]


def cold_sim(ring_oc: int = 0x28) -> SimMsrPort:
    cfg = SimConfig.measured(per_point=True, reject_zero_fields=True, sticky_oc_ratio=True)
    return SimMsrPort(dataclasses.replace(cfg, initial_domain_vf={**cfg.initial_domain_vf, 2: VfWord.from_u32(ring_oc)}))


def ring_requests(sim: SimMsrPort, cmd: int | None = None) -> list:
    return [r for r in sim.requests if r.p1 == 2 and (cmd is None or r.cmd == cmd)]


class RingCliCase(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.policy_file = self.write("shipped.toml", SHIPPED)
        self.wall = 1_700_000_000.0
        self.sim = cold_sim()

    def run_cli(self, *argv: object, **kwargs):
        """Each invocation is a later process: the audit log of the earlier ones must lie in the past (the guard refuses a dwell it cannot evaluate)."""
        self.wall += 10_000.0
        now = self.wall
        kwargs.setdefault("wall_clock", lambda: now)
        return super().run_cli(*argv, **kwargs)

    def dump(self, *extra: object, sim: SimMsrPort | None = None, json_name: str | None = "dump.json"):
        args = ["vf-dump", "--sim", "--policy", self.policy_file, "--audit", self.audit, "--lock", self.lock, *extra]
        if json_name is not None:
            args += ["--json", self.tmp / json_name]
        return self.run_cli(*args, stdin=self.confirmed(), sim=sim if sim is not None else self.sim)

    def expected_file(self, ring: list[int] | None = RING_ACCEPTED, name: str = "EXPECTED-words.txt") -> Path:
        lines = [(EXAMPLE / "EXPECTED-words.txt").read_text().rstrip("\n")]
        if ring is not None:
            lines.append(format_ring_line(ring))
        return self.write(name, "\n".join(lines) + "\n")

    def accepted_profile(self) -> Path:
        base = load_profile((EXAMPLE / "accepted-2026-10-04.toml").read_text())
        return self.write("accepted.toml", dump_profile(dataclasses.replace(base, ring=_ring_of(RING_ACCEPTED)), "G-154 test"))

    def compare(self, readback: str = "dump.json", expected: Path | None = None):
        return self.run_cli("compare-accepted", "--readback", self.tmp / readback, "--expected", expected or self.expected_file())

    def apply(self, sim: SimMsrPort | None = None):
        return self.run_cli("apply", self.accepted_profile(), "--sim", "--policy", self.policy_file, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed(), sim=sim if sim is not None else self.sim)


class VfDumpRing(RingCliCase):
    def test_the_ring_is_dumped_by_default_in_text_and_json(self) -> None:
        code, out, err = self.dump()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("Ring (cache, domain P1=2)", out)
        self.assertIn("table end: rc 0x11 at P2=10", out)
        self.assertIn("OC word 0x00000028", out)
        doc = json.loads((self.tmp / "dump.json").read_text())
        ring = doc["ring"]
        self.assertEqual((ring["unit"], ring["cpu"], ring["oc"]["word"], ring["end"]), ("ring", 0, "0x00000028", "rc 0x11 at P2=10"))
        self.assertEqual([p["ratio"] for p in ring["points"]], [8, 14, 20, 28, 34, 37, 38, 39, 40])
        self.assertEqual([r.p2 for r in ring_requests(self.sim, 0x10)], list(range(11)))  # P2 = 0 .. 10, one at a time
        self.assertEqual(ring_requests(self.sim, 0x11), [])
        self.assertIn("+ the ring", out)

    def test_no_ring_and_cpus_leave_the_ring_out(self) -> None:
        for extra in (("--no-ring",), ("--cpus", "0")):
            code, out, err = self.dump(*extra)
            self.assertEqual((code, err), (0, ""), out)
            self.assertNotIn("Ring (cache", out)
            self.assertEqual(ring_requests(self.sim), [], extra)
            self.assertNotIn("ring", json.loads((self.tmp / "dump.json").read_text()))

    def test_a_policy_that_cannot_read_the_ring_leaves_it_out_with_a_note_and_the_cores_stand(self) -> None:
        core_only = self.write("core.toml", SHIPPED.replace('name = "ring_point_read"', 'name = "xx_unused"').replace("p1 = [2]\np2 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]", "p1 = [9]\np2 = [1]", 1))
        self.policy_file = core_only
        code, out, err = self.dump()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("ring not dumped", out)
        self.assertEqual(ring_requests(self.sim), [])
        doc = json.loads((self.tmp / "dump.json").read_text())
        self.assertIn("ring_note", doc)
        self.assertEqual(len(doc["units"]), 12)


class CompareAccepted(RingCliCase):
    """The restore scripts' verdict: the ring line is compared word for word; a file without it compares nothing about the ring."""

    def restored(self) -> None:
        code, out, err = self.apply()
        self.assertEqual((code, err), (0, ""), out)
        code, out, err = self.dump()
        self.assertEqual((code, err), (0, ""), out)

    def test_a_restored_ring_is_ok(self) -> None:
        self.restored()
        code, out, err = self.compare()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("ring   OK  28 8 E 14 1C 22 25 26 FCC00027 FCC00028", out)
        self.assertIn("cores/clusters and the ring match the accepted state", out)

    def test_a_ring_that_differs_is_bad_and_the_exit_code_says_so(self) -> None:
        self.restored()
        code, out, _ = self.compare(expected=self.expected_file(RING_ACCEPTED[:-1] + [0xFCC00029]))
        self.assertEqual(code, 2, out)
        self.assertIn("ring   BAD", out)
        self.assertIn("STOP: the ring differs", out)
        self.assertNotIn("BAD 39", out)

    def test_the_ring_oc_word_differing_is_named(self) -> None:
        self.restored()
        code, out, _ = self.compare(expected=self.expected_file([0x27] + RING_ACCEPTED[1:-1]))
        self.assertEqual(code, 2, out)
        self.assertIn("the ring OC word differs", out)

    def test_a_readback_without_a_ring_is_bad_when_the_accepted_state_has_one(self) -> None:
        self.restored()
        self.dump("--no-ring")
        code, out, _ = self.compare()
        self.assertEqual(code, 2, out)
        self.assertIn("ring   BAD <missing", out)

    def test_an_accepted_file_without_a_ring_line_compares_nothing_about_the_ring(self) -> None:
        self.restored()
        code, out, err = self.compare(expected=self.expected_file(None))
        self.assertEqual((code, err), (0, ""), out)
        self.assertNotIn("ring", out)
        self.assertIn("RESTORED: all 12 cores/clusters match the accepted state.", out)

    def test_a_core_and_the_ring_both_wrong_are_both_reported(self) -> None:
        self.restored()
        words = (EXAMPLE / "EXPECTED-words.txt").read_text().replace("\n1  0 ", "\n1  7 ")
        path = self.write("e.txt", words.rstrip("\n") + "\n" + format_ring_line(RING_ACCEPTED[:-1] + [1]) + "\n")
        code, out, _ = self.compare(expected=path)
        self.assertEqual(code, 2, out)
        self.assertIn("1 core(s)/cluster(s) and the ring differ", out)


class ApplyAccepted(RingCliCase):
    def test_cold_boot_apply_writes_ring_p8_and_p9_with_readback_and_never_the_oc_word(self) -> None:
        sim = self.sim
        code, out, err = self.apply(sim)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("Result: OK", out)
        writes = ring_requests(sim, 0x11)
        self.assertEqual(sorted((r.p2, r.data) for r in writes), [(8, 0xFCC00027), (9, 0xFCC00028)])
        self.assertEqual(sim.vf(0, 2, 0).to_u32(), 0x28)
        self.assertIn("write ring point 8 offset of Ring", out)
        self.assertIn("write ring point 9 offset of Ring", out)
        self.assertIn("Ring point 9", out)
        code, out, err = self.dump()
        self.assertEqual(code, 0, out)
        code, out, err = self.compare()
        self.assertEqual((code, err), (0, ""), out)

    def test_warm_boot_with_everything_held_writes_nothing(self) -> None:
        sim = self.sim
        self.apply(sim)
        before = len(sim.requests)
        code, out, err = self.apply(sim)
        self.assertEqual((code, err), (0, ""), out)
        self.assertFalse([r for r in sim.requests[before:] if r.cmd in (0x11, 0x15, 0x1D, 0x27)], "all held: 0 writes")

    def test_the_ring_oc_word_changed_refuses_the_ring_alone_and_the_compare_says_so(self) -> None:
        sim = cold_sim(ring_oc=0x27)  # the BIOS ring ratio is 39: OC word 0x27, no P2 = 9
        code, out, err = self.apply(sim)
        self.assertEqual((code, err), (0, ""), out)  # the P/E units were restored: the apply itself is a success
        self.assertIn("RING NOT RESTORED", out)
        self.assertIn("ring_oc_differs", out)
        self.assertEqual(ring_requests(sim, 0x11), [])
        self.assertTrue([r for r in sim.requests if r.cmd == 0x11 and r.p1 == 0], "the P/E units were written")
        code, out, _ = self.dump(sim=sim)
        self.assertEqual(code, 0, out)
        code, out, _ = self.compare()
        self.assertEqual(code, 2, out)
        self.assertIn("STOP: the ring differs - report; the cores/clusters match", out)
        self.assertIn("the ring OC word differs", out)

    def test_an_accepted_profile_without_a_ring_never_touches_the_ring(self) -> None:
        sim = cold_sim()
        code, out, err = self.run_cli(
            "apply", EXAMPLE / "accepted-2026-10-04.toml", "--sim", "--policy", self.policy_file, "--audit", self.audit, "--lock", self.lock, stdin=self.confirmed(), sim=sim
        )
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(ring_requests(sim), [])

    def test_a_policy_that_cannot_read_the_ring_points_refuses_the_ring_alone_and_the_cores_are_restored(self) -> None:
        no_read = SHIPPED.replace('name = "ring_point_read"', 'name = "xx_ring_read_gone"').replace("p1 = [2]\np2 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]\nkind = \"read\"", "p1 = [9]\np2 = [1]\nkind = \"read\"", 1)
        self.assertNotEqual(no_read, SHIPPED)
        self.policy_file = self.write("noread.toml", no_read)
        sim = self.sim
        code, out, err = self.apply(sim)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("RING NOT RESTORED", out)
        self.assertIn("was not read: the guard refuses the read (NOT_ALLOWLISTED", out)
        self.assertEqual(ring_requests(sim, 0x11), [])
        self.assertTrue([r for r in sim.requests if r.cmd == 0x11 and r.p1 == 0], "the P/E units were written")

    def test_the_policy_refusing_a_ring_write_is_a_ring_refusal_with_the_guards_reason_not_a_silent_skip(self) -> None:
        no_write = SHIPPED.replace("p2 = [1, 2, 3, 4, 5, 6, 7, 8, 9]\nkind = \"write\"\ncheck = \"vf_point_offset_bounded\"", "p2 = [1, 2, 3, 4, 5, 6, 7]\nkind = \"write\"\ncheck = \"vf_point_offset_bounded\"")
        self.assertNotEqual(no_write, SHIPPED)
        self.policy_file = self.write("short.toml", no_write)  # this policy lets ring P2 1..7 through, not 8 and 9
        sim = cold_sim()
        code, out, err = self.apply(sim)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("RING NOT RESTORED", out)
        self.assertIn("NOT_ALLOWLISTED", out)
        self.assertEqual(ring_requests(sim, 0x11), [])
        code, _, _ = self.dump(sim=sim)
        self.assertEqual(self.compare()[0], 2)
