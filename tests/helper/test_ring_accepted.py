"""G-154 (D-045): the ring (cache, mailbox domain P1 = 2) is part of the read, the accepted state and the restore.

Simulator flows through the real helper loop and the REAL guard (no hardware), with the live words of the reference machine (2026-10-15): ring OC word ``0x28``, points
8/14/20/28/34/37/38/39x and ``P2 = 9`` = 40x (present while the BIOS ring OC ratio is 40); accepted: point 8 ``0xFCC00027`` and point 9 ``0xFCC00028`` (both -25.39 mV).

(a) cold boot (ring offsets 0) -> Restore accepted writes ring P2 8 and 9 with read-back, the P/E units as before, compare OK incl. the ring; (b) warm boot, everything held -> no write;
(c) the ring OC word changed (0x27: no P2 = 9) -> the ring unit alone is refused by name, P/E restored, the compare reports the ring BAD; (d) an accepted state without a ring line ->
nothing about the ring is read for a write, written or compared; (e) ``profiles.accept {from: "read"}`` writes the ring line and the ring section and a re-read compares equal.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from arltuner.mailbox import VfWord
from arltuner.profiles import load_profile
from arltuner.profiles.complete import _ring_of
from arltuner.profiles.expected import format_ring_line, parse_expected
from arltuner.profiles.serialize import dump_profile

from .test_complete_profiles import COLD, EXPECTED, CompleteCase
from .test_accept import AcceptCase

#: [MEASURED lnx 2026-10-15] ring words now: OC word, points 1..8 (point 8 = 39x with -25.39 mV), P2 = 9 (40x, -25.39 mV).
RING_ACCEPTED = [0x28, 0x08, 0x0E, 0x14, 0x1C, 0x22, 0x25, 0x26, 0xFCC00027, 0xFCC00028]
WARM_RING_OC_27 = dataclasses.replace(COLD, initial_domain_vf={**COLD.initial_domain_vf, 2: VfWord.from_u32(0x27)})  # the BIOS ring ratio is 39 again: no P2 = 9


def ring_requests(sim: Any, cmd: int | None = None, since: int = 0) -> list[Any]:
    return [r for r in sim.requests[since:] if r.p1 == 2 and (cmd is None or r.cmd == cmd)]


class RingCase(CompleteCase):
    def accepted_with_ring(self, ring: list[int] | None = RING_ACCEPTED, line: bool = True, section: bool = True) -> Path:
        """The reference machine's accepted state plus the ring: a ``[ring]`` section in the profile and a ``ring`` line in EXPECTED-words.txt."""
        acc = self.accepted()
        if ring is not None and section:
            path = acc / "profiles" / "accepted-2026-10-04.toml"
            path.write_text(dump_profile(dataclasses.replace(load_profile(path.read_text()), ring=_ring_of(ring)), "G-154 test"))
        if ring is not None and line:
            words = acc / "EXPECTED-words.txt"
            words.write_text(words.read_text() + format_ring_line(ring) + "\n")
        return acc


class ColdBoot(RingCase):
    def test_the_read_returns_the_ring_in_the_shape_of_a_unit(self) -> None:
        h = self.boot(COLD, accepted_dir=self.accepted_with_ring())
        res = self.read(h)
        ring = res["ring"]
        self.assertEqual((ring["unit"], ring["cpu"], ring["end"]), ("ring", 0, "rc 0x11 at P2=10"))
        self.assertEqual(ring["oc"]["word"], "0x00000028")
        self.assertEqual([p["ratio"] for p in ring["points"]], [8, 14, 20, 28, 34, 37, 38, 39, 40])  # P2 = 9 is the 40x slot the BIOS ring OC ratio creates
        self.assertEqual([p["offset_raw"] for p in ring["points"]], [0] * 9)  # cold boot: runtime offsets 0
        self.assertEqual(res["ops_ring"], 11)  # P2 = 0 .. 10
        self.assertEqual(res["ops_done"], res["ops_words"] + res["ops_extras"] + res["ops_ring"])
        self.assertEqual(h.helper.last_ring, [0x28, 0x08, 0x0E, 0x14, 0x1C, 0x22, 0x25, 0x26, 0x27, 0x28])  # the CPU-keyed words are untouched
        self.assertEqual(len(h.helper.last_words[0]), 9)

    def test_restore_writes_ring_points_8_and_9_with_readback_and_the_ring_oc_word_never(self) -> None:
        h = self.boot(COLD, accepted_dir=self.accepted_with_ring())
        self.read(h)
        plan, result = self.restore(h)
        self.assertEqual(sorted((s["unit"], s["point"]) for s in plan["steps"] if s["kind"] == "write_ring_point"), [("ring", 8), ("ring", 9)])
        self.assertIn("ring", plan["units"])
        self.assertEqual(plan["after"]["ring"]["8"]["offset_mv"], -25.391)
        writes = ring_requests(self.sim, 0x11)
        self.assertEqual(sorted((r.cpu, r.p2, r.data) for r in writes), [(0, 8, 0xFCC00027), (0, 9, 0xFCC00028)])
        self.assertNotIn(0, [r.p2 for r in writes], "the ring OC word is BIOS-owned: never written")
        self.assertEqual(self.sim.vf(0, 2, 8).to_u32(), 0xFCC00027)
        self.assertEqual(self.sim.vf(0, 2, 9).to_u32(), 0xFCC00028)
        self.assertEqual(self.sim.vf(0, 2, 0).to_u32(), 0x28)
        ring_rows = [r for r in result["readback"] if r["unit"] == "ring"]
        self.assertEqual([(r["point"], r["expected"], r["actual"], r["match"]) for r in ring_rows], [(8, "0xfcc00027", "0xfcc00027", True), (9, "0xfcc00028", "0xfcc00028", True)])
        self.assertEqual(result["ring_refused"], [])
        self.assert_expected_words()  # the P/E units as before
        self.assertEqual(f"{self.sim.vf(0, 0, 8).to_u32():X}", f"{EXPECTED[0][8]:X}")  # P-core 0 point 8 is its own word, not ring point 8 (no CPU-key collision)
        again = self.read(h)
        ring_entry = next(c for c in again["accepted_match"] if c["cpu"] == "ring")
        self.assertEqual(ring_entry, {"cpu": "ring", "unit": "ring", "match": True, "diffs": []})
        self.assertTrue(all(c["match"] for c in again["accepted_match"]), again["accepted_match"])

    def test_the_mailbox_traffic_is_one_transaction_at_a_time_and_reads_precede_writes(self) -> None:
        h = self.boot(COLD, accepted_dir=self.accepted_with_ring())
        self.read(h)
        start = len(self.sim.requests)
        self.restore(h)
        apply_requests = self.sim.requests[start:]
        first_write = next(i for i, r in enumerate(apply_requests) if r.cmd in (0x11, 0x15))
        self.assertTrue(all(r.cmd not in (0x11, 0x15) for r in apply_requests[:first_write]))
        # the ring reads of the apply: the OC word and the nine points, all before the first write (then the guard's read-back of each of the two ring writes)
        reads = [(i, r.p2) for i, r in enumerate(apply_requests) if r.p1 == 2 and r.cmd == 0x10]
        self.assertEqual([p2 for _, p2 in reads], [*range(10), 8, 9])
        self.assertLess(reads[9][0], first_write)


class WarmBoot(RingCase):
    def test_everything_held_is_zero_writes_and_a_clean_compare(self) -> None:
        h = self.boot(COLD, accepted_dir=self.accepted_with_ring())
        self.read(h)
        self.restore(h)
        self.read(h)
        before = len(self.sim.requests)
        plan, result = self.restore(h)
        self.assertEqual([s for s in plan["steps"] if s["kind"] == "write_ring_point"], [])
        self.assertEqual(result["writes_sent"], 0)
        self.assertEqual(result["ring_refused"], [])
        self.assertFalse([r for r in self.sim.requests[before:] if r.cmd in (0x11, 0x15, 0x1D, 0x27)])
        after = self.read(h)
        self.assertTrue(all(c["match"] for c in after["accepted_match"]), after["accepted_match"])

    def test_a_ring_that_holds_the_accepted_offsets_already_is_not_written_again(self) -> None:
        cfg = dataclasses.replace(COLD, ring_point_offsets={8: VfWord.from_u32(0xFCC00027).offset_raw, 9: VfWord.from_u32(0xFCC00028).offset_raw})
        h = self.boot(cfg, accepted_dir=self.accepted_with_ring())
        self.read(h)
        _, result = self.restore(h)
        self.assertEqual(ring_requests(self.sim, 0x11), [])
        self.assertTrue(result["complete"])


class RingOcWordChanged(RingCase):
    """(c) The BIOS ring OC ratio is 39 again (OC word 0x27, no P2 = 9): the ring unit alone is refused, by name; the P/E units are still restored."""

    def test_the_ring_is_refused_by_name_the_cores_are_restored_and_the_compare_names_the_ring(self) -> None:
        h = self.boot(WARM_RING_OC_27, accepted_dir=self.accepted_with_ring())
        res = self.read(h)
        self.assertEqual(res["ring"]["oc"]["word"], "0x00000027")
        self.assertEqual(len(res["ring"]["points"]), 8)
        plan = self.plan(h, {"restore": "accepted"})
        ring_warns = [w for w in plan["warnings"] if w["code"] == "RING_NOT_RESTORED"]
        self.assertTrue(any("ring_oc_differs" in w["text"] for w in ring_warns), plan["warnings"])
        self.assertTrue(any("ring point 9 does not exist" in w["text"] for w in ring_warns), plan["warnings"])  # rc 0x11: no 40x slot while the OC ratio is 39
        self.assertEqual([s for s in plan["steps"] if s["kind"] == "write_ring_point"], [])
        result = self.apply_ok(h, plan)
        self.assertTrue(any("ring_oc_differs" in why for why in result["ring_refused"]), result["ring_refused"])
        self.assertTrue(any("RING NOT RESTORED" in w for w in result["warnings"]), result["warnings"])
        self.assertEqual(ring_requests(self.sim, 0x11), [], "no ring word is written")
        self.assert_expected_words()  # the P/E units were restored
        after = self.read(h)
        bad = [c for c in after["accepted_match"] if not c["match"]]
        self.assertEqual([c["cpu"] for c in bad], ["ring"])
        self.assertEqual(bad[0]["diffs"][0], {"point": 0, "expected": "0x00000028", "actual": "0x00000027"})

    def test_a_ring_point_whose_ratio_is_not_the_expected_one_refuses_the_ring_alone(self) -> None:
        cfg = dataclasses.replace(COLD, ring_point_ratios={**COLD.ring_point_ratios, 7: 36})  # point 7 reads 36x, the accepted state expects 38x
        h = self.boot(cfg, accepted_dir=self.accepted_with_ring())
        self.read(h)
        _, result = self.restore(h)
        self.assertTrue(any("expect_ratio" in why and "ring point 7" in why for why in result["ring_refused"]), result["ring_refused"])
        self.assertEqual(ring_requests(self.sim, 0x11), [])
        self.assert_expected_words()


class NoRingInTheAcceptedState(RingCase):
    """(d) An accepted state without a ring line: the ring is neither compared, nor read for a write, nor written."""

    def test_nothing_about_the_ring_is_planned_written_or_compared(self) -> None:
        h = self.boot(COLD, accepted_dir=self.accepted())
        self.read(h)
        start = len(self.sim.requests)
        plan, result = self.restore(h)
        self.assertNotIn("ring", plan["units"])
        self.assertEqual(ring_requests(self.sim, None, start), [])
        self.assertEqual(result["ring_refused"], [])
        self.assert_expected_words()
        after = self.read(h)
        self.assertTrue(all(c["cpu"] != "ring" for c in after["accepted_match"]), after["accepted_match"])
        self.assertTrue(all(c["match"] for c in after["accepted_match"]))
        self.assertIsNotNone(after["ring"])  # it is still READ (and shown): only the compare and the restore leave it alone

    def test_an_older_profile_without_the_section_loads_and_a_ring_line_alone_changes_nothing_but_the_compare(self) -> None:
        acc = self.accepted_with_ring(section=False)  # EXPECTED-words.txt has the line, the profile has no [ring]: the restore writes none, the compare still judges the ring
        h = self.boot(COLD, accepted_dir=acc)
        self.read(h)
        _, result = self.restore(h)
        self.assertEqual(ring_requests(self.sim, 0x11), [])
        after = self.read(h)
        self.assertEqual([c["cpu"] for c in after["accepted_match"] if not c["match"]], ["ring"])  # honest: the accepted ring is not what the CPU holds


class AcceptFromRead(AcceptCase):
    """(e) ``profiles.accept {from: "read"}`` records the ring line and the ring section; a re-read compares equal."""

    def test_the_ring_line_and_section_are_written_and_a_reread_compares_equal(self) -> None:
        h = self.boot_accepting()  # ALL_P_53 is a measured() simulator: the ring is there
        # put the live ring words in place with the CLI-equivalent runtime writes (point 8 and the OC-created slot, -25 mV), then read
        self.sim._vf[(2, -1, 8)] = VfWord.from_u32(0xFCC00027)
        self.sim._vf[(2, -1, 9)] = VfWord.from_u32(0xFCC00028)
        self.read(h)
        r = self.accept(h, **{"from": "read"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["ring_points"], 9)
        files = self.accepted_files()
        table, ring = parse_expected(files["EXPECTED-words.txt"])
        self.assertEqual(ring, RING_ACCEPTED)
        self.assertIn("ring  28 8 E 14 1C 22 25 26 FCC00027 FCC00028", files["EXPECTED-words.txt"])
        name = next(p for p in files if p.startswith("profiles/"))
        profile = load_profile(files[name])
        self.assertTrue(profile.complete)
        self.assertEqual(profile.ring.oc_word, 0x28)
        self.assertEqual([(p.point, p.offset_mv, p.expect_ratio) for p in profile.ring.points][-2:], [(8, -25, 39), (9, -25, 40)])
        after = self.read(h)
        self.assertTrue(all(c["match"] for c in after["accepted_match"]), after["accepted_match"])
        self.assertEqual(next(c for c in after["accepted_match"] if c["cpu"] == "ring")["diffs"], [])
        self.assertEqual(sorted(table), [*range(8), 8, 12, 16, 20])
