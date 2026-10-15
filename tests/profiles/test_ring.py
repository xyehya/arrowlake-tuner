"""G-154 (D-045): the ring (cache, mailbox domain P1 = 2) in the profile layer -- the ``[ring]`` schema, the TOML round trip, the complete profile of ring words, the
``EXPECTED-words.txt`` ``ring`` line (one parser for every reader) and ``accepted-migrate``. Pure; no hardware."""
from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from arltuner.helper.ops import _parse_expected
from arltuner.mailbox import VfWord
from arltuner.profiles import BuildError, CoreSetting, Profile, ProfileError, RingPoint, RingSetting, complete_from_words, load_profile, overlay
from arltuner.profiles.complete import _ring_of
from arltuner.profiles.expected import format_ring_line, parse_expected
from arltuner.profiles.migrate import MigrateError, parse_expected as migrate_parse_expected
from arltuner.profiles.migrate import parse_expected_with_ring
from arltuner.profiles.serialize import dump_profile
from arltuner.topology import arl_s_285k

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples/reference-285k/profiles"
RING = [0x28, 0x08, 0x0E, 0x14, 0x1C, 0x22, 0x25, 0x26, 0xFCC00027, 0xFCC00028]
TOPO = arl_s_285k()
BASE = '[meta]\nname = "t"\nplatform = "arl-s"\n\n[[core]]\nkind = "P"\nindex = 4\noffset_mv = -30\n'


def ring_toml(oc: str = "0x00000028", points: str = "") -> str:
    return BASE + f"\n[ring]\noc_word = {oc}\n" + points


def ring_point(n: int, mv: int, ratio: int) -> str:
    return f"\n[[ring.point]]\npoint = {n}\noffset_mv = {mv}\nexpect_ratio = {ratio}\n"


class Schema(unittest.TestCase):
    def test_a_ring_section_loads_and_round_trips(self) -> None:
        text = ring_toml(points=ring_point(8, -25, 39) + ring_point(9, -25, 40))
        profile = load_profile(text)
        self.assertEqual(profile.ring, RingSetting(0x28, (RingPoint(8, -25, 39), RingPoint(9, -25, 40))))
        again = load_profile(dump_profile(profile))
        self.assertEqual(again, profile)
        self.assertIn("oc_word = 0x00000028", dump_profile(profile))

    def test_a_profile_without_a_ring_is_unchanged_and_valid(self) -> None:
        profile = load_profile(BASE)
        self.assertIsNone(profile.ring)
        self.assertNotIn("[ring]", dump_profile(profile))

    def test_a_ring_alone_is_a_valid_profile(self) -> None:
        text = '[meta]\nname = "r"\nplatform = "arl-s"\n\n[ring]\noc_word = 40\n' + ring_point(8, -25, 39)
        self.assertEqual(load_profile(text).ring.oc_word, 40)

    def test_bad_rings_are_refused_with_the_key_named(self) -> None:
        cases = {
            "oc_word missing": BASE + "\n[ring]\n" + ring_point(8, -25, 39),
            "unknown key": ring_toml(points="").replace("oc_word = 0x00000028", "oc_word = 40\nfoo = 1"),
            "expect_ratio missing": ring_toml(points="\n[[ring.point]]\npoint = 8\noffset_mv = -25\n"),
            "duplicate point": ring_toml(points=ring_point(8, -25, 39) + ring_point(8, -26, 39)),
            "ratios do not rise": ring_toml(points=ring_point(8, -25, 39) + ring_point(9, -25, 39)),
            "offset out of range": ring_toml(points=ring_point(8, -1001, 39)),
            "oc_word too big": ring_toml(oc="0x100000000"),
            "point 0": ring_toml(points=ring_point(0, 0, 39)),
        }
        for label, text in cases.items():
            with self.assertRaises(ProfileError, msg=label):
                load_profile(text)

    def test_a_complete_profile_lists_every_ring_point_without_a_gap(self) -> None:
        base = load_profile((EXAMPLE / "accepted-2026-10-04.toml").read_text())
        with self.assertRaises(ProfileError):
            dataclasses.replace(base, ring=RingSetting(0x28, (RingPoint(1, 0, 8), RingPoint(3, 0, 20))))  # point 2 missing
        self.assertTrue(dataclasses.replace(base, ring=_ring_of(RING)).complete)


class Complete(unittest.TestCase):
    def test_ring_words_become_the_ring_unit_with_every_point_including_the_oc_created_slot(self) -> None:
        ring = _ring_of(RING)
        self.assertEqual(ring.oc_word, 0x28)
        self.assertEqual(
            [(p.point, p.offset_mv, p.expect_ratio) for p in ring.points],
            [(1, 0, 8), (2, 0, 14), (3, 0, 20), (4, 0, 28), (5, 0, 34), (6, 0, 37), (7, 0, 38), (8, -25, 39), (9, -25, 40)],
        )

    def test_no_ring_words_or_no_point_is_no_ring_section(self) -> None:
        self.assertIsNone(_ring_of(None))
        self.assertIsNone(_ring_of([0x28]))
        self.assertIsNone(_ring_of([0x28, 0]))

    def test_an_offset_whole_millivolts_cannot_hold_is_refused_not_rounded(self) -> None:
        with self.assertRaises(BuildError) as cm:
            _ring_of([0x28, VfWord(8, 0, False, 0).to_u32(), VfWord(14, 0, False, 21).to_u32()])  # raw 21 is 20.5 mV: the nearest whole mV (21) writes raw 22
        self.assertEqual(cm.exception.code, "NOT_REPRESENTABLE")

    def test_complete_from_words_carries_the_ring_and_overlay_keeps_it(self) -> None:
        table, ring = parse_expected_with_ring((EXAMPLE / "EXPECTED-words.txt").read_text() + format_ring_line(RING) + "\n")
        tops = {cpu: (57 if cpu < 8 else 46) for cpu in table}
        profile = complete_from_words(TOPO, "x", table, tops, None, ring)
        self.assertTrue(profile.complete)
        self.assertEqual(profile.ring, _ring_of(RING))
        edits = Profile("e", (CoreSetting("P", 4, offset_mv=-10),))
        self.assertEqual(overlay(profile, edits, TOPO).ring, profile.ring)
        self.assertEqual(load_profile(dump_profile(profile)).ring, profile.ring)
        self.assertIsNone(complete_from_words(TOPO, "x", table, tops).ring)


class ExpectedLine(unittest.TestCase):
    TEXT = "# c\n0  39 8 14 1E\n8  0 8 F\nring  28 8 E 14 FCC00027\n"

    def test_every_parser_accepts_the_ring_line(self) -> None:
        table, ring = parse_expected(self.TEXT)
        self.assertEqual((sorted(table), ring), ([0, 8], [0x28, 8, 0xE, 0x14, 0xFCC00027]))
        self.assertEqual(sorted(_parse_expected(self.TEXT)), [0, 8])  # the helper's compare (ops.accepted_match) reads the core lines and leaves the ring to ops_tune
        self.assertEqual(sorted(migrate_parse_expected(self.TEXT)), [0, 8])
        self.assertEqual(parse_expected_with_ring(self.TEXT)[1], ring)

    def test_a_file_without_the_line_has_no_ring(self) -> None:
        self.assertIsNone(parse_expected("0  39 8 14\n")[1])
        self.assertIsNone(parse_expected_with_ring("0  39 8 14\n")[1])

    def test_the_line_is_the_shape_of_a_core_line(self) -> None:
        self.assertEqual(format_ring_line(RING), "ring  28 8 E 14 1C 22 25 26 FCC00027 FCC00028")
        self.assertEqual(parse_expected("0 1 2\n" + format_ring_line(RING) + "\n")[1], RING)

    def test_malformed_ring_lines_are_refused(self) -> None:
        for text in ("0 1 2\nring\n", "0 1 2\nring 28\n", "0 1 2\nring 28 zz\n", "0 1 2\nring 28 8\nring 28 8\n", "0 1 2\nring 1FFFFFFFF 8\n"):
            with self.assertRaises(ValueError, msg=text):
                parse_expected(text)
        with self.assertRaises(MigrateError):
            parse_expected_with_ring("0 1 2\nring 28\n")
        with self.assertRaises(ValueError):
            parse_expected("ring 28 8\n")  # a ring alone is no accepted state


class Migrate(unittest.TestCase):
    def test_a_migration_keeps_the_ring_line_and_adds_the_ring_section(self) -> None:
        import shutil
        import tempfile

        from arltuner.profiles.migrate import plan_migration

        with tempfile.TemporaryDirectory() as tmp:
            acc = Path(tmp) / "accepted"
            (acc / "profiles").mkdir(parents=True)
            for name in ("e-p6-split3.toml", "p-boot-ramp.toml", "p-step7.toml"):
                shutil.copy(EXAMPLE / name, acc / "profiles" / name)
            (acc / "CURRENT").write_text("".join(f"profile = profiles/{n}.toml\n" for n in ("e-p6-split3", "p-boot-ramp", "p-step7")))
            (acc / "EXPECTED-words.txt").write_text((EXAMPLE / "EXPECTED-words.txt").read_text() + format_ring_line(RING) + "\n")
            migration = plan_migration(acc, TOPO, "20261015T000000Z")
            self.assertEqual(migration.profile.ring, _ring_of(RING))
            self.assertIn("[ring]", migration.profile_toml)
            self.assertIn("ring  28 8 E", migration.expected_text)  # EXPECTED-words.txt unchanged, byte for byte


if __name__ == "__main__":
    unittest.main()
