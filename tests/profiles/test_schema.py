"""Tests for arltuner.profiles (T-012). Files only ever live in temp directories."""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import inspect
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import arltuner.profiles as profiles_pkg
from arltuner.profiles import CoreSetting, CpuSetting, Profile, ProfileError, TurboTables, load_profile
from arltuner.topology import Topology, arl_s_285k, synthetic

HEADER = '[meta]\nname = "t"\nplatform = "arl-s-285k"\n'


def core(body: str) -> str:
    return "\n[[core]]\n" + body.strip() + "\n"


# --- the three valid example profiles -----------------------------------------------------------------------

EXAMPLE_P_ONLY = """\
[meta]
name = "p-only-offsets"
platform = "arl-s-285k"

[[core]]
kind = "P"
index = 0
offset_mv = -30

[[core]]
kind = "P"
index = 3
offset_mv = -25

[[core]]
kind = "P"
index = 4
offset_mv = -40
"""

EXAMPLE_E_CLUSTER = """\
[meta]
name = "e-cluster"
platform = "arl-s-285k"

[[core]]
kind = "E"
index = 12
ratio = 46
offset_mv = -50

[[core]]
kind = "E"
index = 13
ratio = 46
offset_mv = -50
"""

EXAMPLE_MIXED = """\
[meta]
name = "mixed"
platform = "arl-s-285k"

[[core]]
kind = "P"
index = 4
ratio = 55
oc_voltage_mv = 1300
offset_mv = -30

[[core]]
kind = "E"
index = 0
offset_mv = -20

[[core]]
kind = "E"
index = 9
ratio = 46
"""


class ValidProfileTests(unittest.TestCase):
    def test_p_only_offsets(self) -> None:
        p = load_profile(EXAMPLE_P_ONLY)
        self.assertEqual(p.name, "p-only-offsets")
        self.assertEqual(
            p.entries,
            (
                CoreSetting("P", 0, None, None, -30),
                CoreSetting("P", 3, None, None, -25),
                CoreSetting("P", 4, None, None, -40),
            ),
        )

    def test_e_cluster(self) -> None:
        p = load_profile(EXAMPLE_E_CLUSTER)
        self.assertEqual([(e.kind, e.index, e.ratio, e.offset_mv) for e in p.entries], [("E", 12, 46, -50), ("E", 13, 46, -50)])

    def test_mixed(self) -> None:
        p = load_profile(EXAMPLE_MIXED)
        self.assertEqual(
            p.entries,
            (
                CoreSetting("P", 4, 55, 1300, -30),
                CoreSetting("E", 0, None, None, -20),
                CoreSetting("E", 9, 46, None, None),
            ),
        )

    def test_boundary_values_are_accepted(self) -> None:
        body = core('kind = "P"\nindex = 7\nratio = 100\noc_voltage_mv = 2000\noffset_mv = 999')
        body += core('kind = "E"\nindex = 15\nratio = 1\noc_voltage_mv = 500\noffset_mv = -1000')
        p = load_profile(HEADER + body)
        self.assertEqual(p.entries[0], CoreSetting("P", 7, 100, 2000, 999))
        self.assertEqual(p.entries[1], CoreSetting("E", 15, 1, 500, -1000))

    def test_single_setting_of_each_kind_is_enough(self) -> None:
        for key, val in (("ratio", 50), ("oc_voltage_mv", 1200), ("offset_mv", -10)):
            with self.subTest(key):
                p = load_profile(HEADER + core(f'kind = "P"\nindex = 1\n{key} = {val}'))
                self.assertEqual(getattr(p.entries[0], key), val)

    def test_entries_and_settings_are_immutable(self) -> None:
        p = load_profile(EXAMPLE_P_ONLY)
        self.assertIsInstance(p.entries, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            p.entries[0].offset_mv = 0  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            p.name = "x"  # type: ignore[misc]

    def test_load_from_path_and_single_line_str_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "p.toml"
            f.write_text(EXAMPLE_MIXED, encoding="utf-8")
            self.assertEqual(load_profile(f), load_profile(EXAMPLE_MIXED))
            self.assertEqual(load_profile(str(f)), load_profile(EXAMPLE_MIXED))

    def test_crlf_text_is_accepted(self) -> None:
        self.assertEqual(load_profile(EXAMPLE_MIXED.replace("\n", "\r\n")), load_profile(EXAMPLE_MIXED))


class ExpandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.topo = arl_s_285k()

    def test_p_entries_are_single_cpu(self) -> None:
        out = load_profile(EXAMPLE_P_ONLY).expand(self.topo)
        self.assertEqual([(s.kind, s.index, s.cpu, s.cpus, s.cluster, s.cluster_wide) for s in out],
                         [("P", 0, 0, (0,), None, False), ("P", 3, 3, (3,), None, False), ("P", 4, 4, (4,), None, False)])
        self.assertEqual([s.offset_mv for s in out], [-30, -25, -40])
        self.assertTrue(all(isinstance(s, CpuSetting) for s in out))

    def test_single_e_entry_applies_to_whole_cluster(self) -> None:
        p = load_profile(HEADER + core('kind = "E"\nindex = 12\noffset_mv = -50'))
        (s,) = p.expand(self.topo)
        self.assertEqual((s.cpu, s.cluster, s.cpus, s.cluster_wide), (20, 3, (20, 21, 22, 23), True))
        self.assertEqual((s.ratio, s.oc_voltage_mv, s.offset_mv), (None, None, -50))

    def test_agreeing_e_entries_share_one_cluster_value(self) -> None:
        out = load_profile(EXAMPLE_E_CLUSTER).expand(self.topo)
        self.assertEqual([s.cpu for s in out], [20, 21])
        self.assertEqual({s.cpus for s in out}, {(20, 21, 22, 23)})
        self.assertEqual({(s.ratio, s.oc_voltage_mv, s.offset_mv) for s in out}, {(46, None, -50)})
        self.assertTrue(all(s.cluster_wide and s.cluster == 3 for s in out))

    def test_mixed_keeps_profile_order_and_values(self) -> None:
        out = load_profile(EXAMPLE_MIXED).expand(self.topo)
        self.assertEqual([(s.kind, s.index, s.cpu, s.cluster_wide) for s in out],
                         [("P", 4, 4, False), ("E", 0, 8, True), ("E", 9, 17, True)])
        self.assertEqual(out[1].cpus, (8, 9, 10, 11))
        self.assertEqual(out[2].cpus, (16, 17, 18, 19))
        self.assertEqual((out[0].ratio, out[0].oc_voltage_mv, out[0].offset_mv), (55, 1300, -30))

    def test_core_missing_from_topology_is_profile_error(self) -> None:
        p_only = Topology(cores=tuple(c for c in self.topo.cores if c.kind == "P"))
        with self.assertRaises(ProfileError) as cm:
            load_profile(EXAMPLE_MIXED).expand(p_only)
        self.assertIn("E-Core 0", str(cm.exception))
        # P-only profile still expands against the P-only topology
        self.assertEqual(len(load_profile(EXAMPLE_P_ONLY).expand(p_only)), 3)


# --- failing cases ------------------------------------------------------------------------------------------

FAILING: list[tuple[str, str, str]] = [
    # (label, profile text, substring that must appear in the ProfileError message)
    ("invalid toml", HEADER + "[[core]]\nkind = \n", "invalid TOML"),
    ("missing meta", core('kind = "P"\nindex = 0\noffset_mv = -10'), "[meta]"),
    ("meta not a table", 'meta = 3\n' + core('kind = "P"\nindex = 0\noffset_mv = -10'), "meta"),
    ("meta unknown key", HEADER + 'author = "me"\n' + core('kind = "P"\nindex = 0\noffset_mv = -10'), "meta: unknown key 'author'"),
    ("meta missing name", '[meta]\nplatform = "arl-s-285k"\n' + core('kind = "P"\nindex = 0\noffset_mv = -10'), "meta: missing required key 'name'"),
    ("meta name empty", '[meta]\nname = "  "\nplatform = "arl-s-285k"\n' + core('kind = "P"\nindex = 0\noffset_mv = -10'), "meta.name"),
    ("meta name not a string", '[meta]\nname = 5\nplatform = "arl-s-285k"\n' + core('kind = "P"\nindex = 0\noffset_mv = -10'), "meta.name"),
    ("meta missing platform", '[meta]\nname = "t"\n' + core('kind = "P"\nindex = 0\noffset_mv = -10'), "meta: missing required key 'platform'"),
    ("platform mismatch", '[meta]\nname = "t"\nplatform = "arl-s-265k"\n' + core('kind = "P"\nindex = 0\noffset_mv = -10'), "meta.platform"),
    ("top-level unknown key", 'extra = 1\n' + HEADER + core('kind = "P"\nindex = 0\noffset_mv = -10'), "unknown key 'extra'"),
    ("empty profile: no core", HEADER, "profile is empty"),
    ("empty profile: empty array", "core = []\n" + HEADER, "profile is empty"),
    ("core is a table not an array", HEADER + '[core]\nkind = "P"\nindex = 0\noffset_mv = -10\n', "core: expected an array"),
    ("core entry is not a table", "core = [1]\n" + HEADER, "core[0]: expected a table"),
    ("core unknown key", HEADER + core('kind = "P"\nindex = 0\noffset_mv = -10\nvoltage = 1'), "core[0]: unknown key 'voltage'"),
    ("kind missing", HEADER + core("index = 0\noffset_mv = -10"), "core[0]: missing required key 'kind'"),
    ("index missing", HEADER + core('kind = "P"\noffset_mv = -10'), "core[0]: missing required key 'index'"),
    ("unknown kind", HEADER + core('kind = "X"\nindex = 0\noffset_mv = -10'), "core[0].kind"),
    ("kind is lower case", HEADER + core('kind = "p"\nindex = 0\noffset_mv = -10'), "core[0].kind"),
    ("kind not a string", HEADER + core('kind = 1\nindex = 0\noffset_mv = -10'), "core[0].kind"),
    ("index out of unit sanity", HEADER + core('kind = "P"\nindex = 256\noffset_mv = -10'), "core[0].index"),
    ("index negative", HEADER + core('kind = "E"\nindex = -1\noffset_mv = -10'), "core[0].index"),
    ("index is bool", HEADER + core('kind = "P"\nindex = true\noffset_mv = -10'), "core[0].index"),
    ("index is float", HEADER + core('kind = "P"\nindex = 1.0\noffset_mv = -10'), "core[0].index"),
    ("ratio is bool", HEADER + core('kind = "P"\nindex = 0\nratio = true'), "core[0].ratio"),
    ("ratio is string", HEADER + core('kind = "P"\nindex = 0\nratio = "55"'), "core[0].ratio"),
    ("ratio is float", HEADER + core('kind = "P"\nindex = 0\nratio = 55.0'), "core[0].ratio"),
    ("ratio zero", HEADER + core('kind = "P"\nindex = 0\nratio = 0'), "core[0].ratio"),
    ("ratio too high", HEADER + core('kind = "P"\nindex = 0\nratio = 101'), "core[0].ratio"),
    ("oc_voltage_mv too low", HEADER + core('kind = "P"\nindex = 0\noc_voltage_mv = 499'), "core[0].oc_voltage_mv"),
    ("oc_voltage_mv too high", HEADER + core('kind = "P"\nindex = 0\noc_voltage_mv = 2001'), "core[0].oc_voltage_mv"),
    ("oc_voltage_mv given in volts", HEADER + core('kind = "P"\nindex = 0\noc_voltage_mv = 1.3'), "core[0].oc_voltage_mv"),
    ("offset_mv too low", HEADER + core('kind = "P"\nindex = 0\noffset_mv = -1001'), "core[0].offset_mv"),
    ("offset_mv too high", HEADER + core('kind = "P"\nindex = 0\noffset_mv = 1000'), "core[0].offset_mv"),
    ("offset_mv is bool", HEADER + core('kind = "P"\nindex = 0\noffset_mv = false'), "core[0].offset_mv"),
    ("offset_mv is string", HEADER + core('kind = "P"\nindex = 0\noffset_mv = "-30"'), "core[0].offset_mv"),
    ("no setting at all", HEADER + core('kind = "P"\nindex = 0'), "sets nothing"),
    (
        "duplicate P entry",
        HEADER + core('kind = "P"\nindex = 2\noffset_mv = -10') + core('kind = "P"\nindex = 2\noffset_mv = -20'),
        "core[1]: duplicate P-Core 2",
    ),
    (
        "duplicate E entry even when identical",
        HEADER + core('kind = "E"\nindex = 5\nratio = 40') + core('kind = "E"\nindex = 5\nratio = 40'),
        "core[1]: duplicate E-Core 5",
    ),
    (
        "cluster disagrees on offset",
        HEADER + core('kind = "E"\nindex = 12\noffset_mv = -50') + core('kind = "E"\nindex = 13\noffset_mv = -40'),
        "cluster 3 disagrees:",
    ),
    (
        "cluster disagrees: value vs unset",
        HEADER + core('kind = "E"\nindex = 0\nratio = 46\noffset_mv = -10') + core('kind = "E"\nindex = 3\noffset_mv = -10'),
        "cluster 0 disagrees:",
    ),
    (
        "cluster disagrees with a later third entry",
        HEADER
        + core('kind = "E"\nindex = 4\nratio = 46')
        + core('kind = "E"\nindex = 5\nratio = 46')
        + core('kind = "E"\nindex = 6\nratio = 47'),
        "cluster 1 disagrees:",
    ),
]


class FailingProfileTests(unittest.TestCase):
    def test_at_least_15_failing_cases_are_covered(self) -> None:
        self.assertGreaterEqual(len(FAILING), 15)
        self.assertEqual(len({label for label, _, _ in FAILING}), len(FAILING))  # labels unique

    def test_each_failing_case_raises_profile_error_naming_the_problem(self) -> None:
        for label, text, fragment in FAILING:
            with self.subTest(label):
                with self.assertRaises(ProfileError) as cm:
                    load_profile(text, arl_s_285k())  # the cluster rule needs a topology (D-036): the unit tests pass the simulated reference CPU
                self.assertIn(fragment, str(cm.exception))

    def test_index_and_cluster_rules_are_judged_against_the_topology_they_meet(self) -> None:
        text = HEADER + core('kind = "P"\nindex = 8\noffset_mv = -10')
        prof = load_profile(text)  # P-Core 8 is sane for the format; there is no such core on an 8-P part
        with self.assertRaisesRegex(ProfileError, "P-Core 8 is not in this topology"):
            prof.expand(arl_s_285k())
        self.assertEqual(prof.expand(synthetic(10, 8))[0].cpu, 8)  # ... but it is core 8 of a 10-P part
        two = load_profile(HEADER + core('kind = "E"\nindex = 0\nratio = 46') + core('kind = "E"\nindex = 3\nratio = 47'))
        with self.assertRaisesRegex(ProfileError, "cluster 0 disagrees"):
            two.expand(arl_s_285k())
        self.assertEqual(len(two.expand(synthetic(8, 16, e_cluster_size=2))), 2)  # clusters of two: E-Cores 0 and 3 are in different clusters

    def test_cluster_rule_does_not_reject_different_clusters(self) -> None:
        text = HEADER + core('kind = "E"\nindex = 3\noffset_mv = -10') + core('kind = "E"\nindex = 4\noffset_mv = -20')
        self.assertEqual(len(load_profile(text).entries), 2)  # 3 is cluster 0, 4 is cluster 1

    def test_missing_file_is_profile_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for arg in (Path(tmp) / "nope.toml", str(Path(tmp) / "nope.toml")):
                with self.subTest(type(arg).__name__):
                    with self.assertRaises(ProfileError):
                        load_profile(arg)

    def test_non_utf8_file_is_profile_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "bad.toml"
            f.write_bytes(b"\xff\xfe\x00")
            with self.assertRaises(ProfileError):
                load_profile(f)

    def test_wrong_argument_type_is_type_error(self) -> None:
        for arg in (None, 5, b"[meta]\n"):
            with self.subTest(repr(arg)):
                with self.assertRaises(TypeError):
                    load_profile(arg)  # type: ignore[arg-type]

    def test_direct_construction_is_validated_too(self) -> None:
        bad: list = [
            lambda: CoreSetting("P", 0),  # nothing set
            lambda: CoreSetting("P", 256, ratio=50),
            lambda: CoreSetting("Q", 0, ratio=50),
            lambda: CoreSetting("P", 0, ratio=True),
            lambda: Profile("x", ()),
            lambda: Profile("", (CoreSetting("P", 0, ratio=50),)),
            lambda: Profile("x", (CoreSetting("P", 0, ratio=50), CoreSetting("P", 0, ratio=51))),
        ]
        for make in bad:
            with self.subTest(inspect.getsource(make).strip()):
                with self.assertRaises(ProfileError):
                    make()

    def test_profile_error_is_value_error(self) -> None:
        self.assertTrue(issubclass(ProfileError, ValueError))


class ImportTests(unittest.TestCase):
    def test_import_has_no_side_effects(self) -> None:
        # Execute a private copy of the module (the real one stays in sys.modules so class identities used by
        # other tests are unaffected) with file access and stdout trapped.
        path = Path(inspect.getsourcefile(profiles_pkg.load_profile) or "")
        name = "_arltuner_schema_import_probe"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        out = io.StringIO()
        sys.modules[name] = module
        try:
            with (
                contextlib.redirect_stdout(out),
                mock.patch("builtins.open", side_effect=AssertionError("open() at import")),
                mock.patch.object(Path, "open", side_effect=AssertionError("Path.open() at import")),
                mock.patch.object(Path, "read_text", side_effect=AssertionError("read_text() at import")),
            ):
                spec.loader.exec_module(module)
        finally:
            del sys.modules[name]
        self.assertEqual(out.getvalue(), "")


class PointEntries(unittest.TestCase):
    """T-025: ``[[point]]`` -- per-V/F-point offsets, distinct from the whole-curve ``[[core]]`` offset."""

    @staticmethod
    def point(body: str) -> str:
        return "\n[[point]]\n" + body.strip() + "\n"

    def test_a_point_only_profile_loads(self) -> None:
        prof = load_profile(HEADER + self.point('kind = "E"\nindex = 8\npoint = 6\noffset_mv = -20\nexpect_ratio = 46'))
        self.assertEqual(prof.entries, ())
        self.assertEqual(prof.points, (profiles_pkg.PointSetting("E", 8, 6, -20, 46),))
        self.assertIsNone(prof.turbo)

    def test_points_and_cores_together(self) -> None:
        prof = load_profile(HEADER + core('kind = "P"\nindex = 4\noffset_mv = -30') + self.point('kind = "P"\nindex = 4\npoint = 8\noffset_mv = -10'))
        self.assertEqual((len(prof.entries), len(prof.points)), (1, 1))
        self.assertIsNone(prof.points[0].expect_ratio)

    def test_the_range_of_a_point_is_the_register_field(self) -> None:
        for kind, point, ok in (("E", 1, True), ("E", 6, True), ("E", 15, True), ("E", 16, False), ("E", 0, False), ("P", 8, True), ("P", 15, True), ("P", 0, False)):
            text = HEADER + self.point(f'kind = "{kind}"\nindex = 0\npoint = {point}\noffset_mv = -5')
            with self.subTest(kind=kind, point=point):
                if ok:
                    load_profile(text)
                else:
                    with self.assertRaisesRegex(ProfileError, r"point\[0\]\.point"):
                        load_profile(text)

    def test_offset_and_expect_ratio_ranges(self) -> None:
        base = 'kind = "E"\nindex = 8\npoint = 6\n'
        for tail, ok in (("offset_mv = -1000", True), ("offset_mv = 999", True), ("offset_mv = -1001", False), ("offset_mv = 1000", False),
                         ("offset_mv = -5\nexpect_ratio = 0", False), ("offset_mv = -5\nexpect_ratio = 101", False), ("offset_mv = -5\nexpect_ratio = 100", True),
                         ("offset_mv = -5.5", False), ("offset_mv = true", False)):
            with self.subTest(tail=tail):
                text = HEADER + self.point(base + tail)
                if ok:
                    load_profile(text)
                else:
                    with self.assertRaises(ProfileError):
                        load_profile(text)

    def test_missing_and_unknown_keys_are_errors(self) -> None:
        for body, fragment in (
            ('kind = "E"\nindex = 8\noffset_mv = -5', "point[0]: missing required key 'point'"),
            ('kind = "E"\nindex = 8\npoint = 6', "point[0]: missing required key 'offset_mv'"),
            ('index = 8\npoint = 6\noffset_mv = -5', "point[0]: missing required key 'kind'"),
            ('kind = "E"\nindex = 8\npoint = 6\noffset_mv = -5\nratio = 46', "point[0]: unknown key 'ratio'"),
            ('kind = "E"\nindex = 8\npoint = 6\noffset_mv = -5\noc_voltage_mv = 1000', "point[0]: unknown key 'oc_voltage_mv'"),
        ):
            with self.subTest(fragment=fragment), self.assertRaisesRegex(ProfileError, fragment.replace("[", r"\[").replace("]", r"\]")):
                load_profile(HEADER + self.point(body))

    def test_point_must_be_an_array_of_tables(self) -> None:
        with self.assertRaisesRegex(ProfileError, "point: expected an array"):
            load_profile(HEADER + '[point]\nkind = "E"\nindex = 8\npoint = 6\noffset_mv = -5\n')

    def test_duplicates_are_refused_but_the_same_point_of_another_core_is_not(self) -> None:
        one = self.point('kind = "P"\nindex = 4\npoint = 8\noffset_mv = -5')
        with self.assertRaisesRegex(ProfileError, "duplicate P-Core 4 point 8"):
            load_profile(HEADER + one + one)
        load_profile(HEADER + one + self.point('kind = "P"\nindex = 4\npoint = 7\noffset_mv = -5') + self.point('kind = "P"\nindex = 5\npoint = 8\noffset_mv = -5'))

    def test_e_entries_of_one_cluster_must_agree_per_point(self) -> None:
        a = self.point('kind = "E"\nindex = 0\npoint = 6\noffset_mv = -20')
        topo = arl_s_285k()
        with self.assertRaisesRegex(ProfileError, "cluster 0 point 6 disagrees"):
            load_profile(HEADER + a + self.point('kind = "E"\nindex = 1\npoint = 6\noffset_mv = -25'), topo)
        with self.assertRaisesRegex(ProfileError, "cluster 0 point 6 disagrees"):
            load_profile(HEADER + a + self.point('kind = "E"\nindex = 1\npoint = 6\noffset_mv = -20\nexpect_ratio = 46'), topo)
        load_profile(HEADER + a + self.point('kind = "E"\nindex = 1\npoint = 6\noffset_mv = -20') + self.point('kind = "E"\nindex = 1\npoint = 5\noffset_mv = -9'), topo)

    def test_empty_profiles_are_refused_but_a_turbo_table_alone_is_a_profile(self) -> None:
        for text in (HEADER, "point = []\n" + HEADER):
            with self.subTest(text=text[-30:]), self.assertRaisesRegex(ProfileError, "profile is empty: at least one"):
                load_profile(text)
        prof = load_profile(HEADER + "[turbo]\ne = [49, 49, 49, 49, 49, 49, 49, 49]\n")  # G-119: turbo-only intents
        self.assertEqual((prof.entries, prof.points), ((), ()))
        self.assertEqual(prof.turbo.e, (49,) * 8)
        self.assertEqual(Profile("t", (), TurboTables(p=(57,) * 8)).entries, ())
        with self.assertRaisesRegex(ProfileError, "profile is empty"):
            Profile("t", ())

    def test_expand_points_resolves_cpus_and_whole_e_clusters(self) -> None:
        prof = load_profile(HEADER + self.point('kind = "E"\nindex = 0\npoint = 6\noffset_mv = -20') + self.point('kind = "P"\nindex = 4\npoint = 8\noffset_mv = -5'))
        e, p = prof.expand_points(arl_s_285k())
        self.assertEqual((e.kind, e.point, e.cpu, e.cluster, e.cpus, e.cluster_wide), ("E", 6, 8, 0, (8, 9, 10, 11), True))
        self.assertEqual((p.kind, p.point, p.cpu, p.cluster, p.cpus, p.cluster_wide), ("P", 8, 4, None, (4,), False))

    def test_profile_dataclass_validates_points(self) -> None:
        with self.assertRaises(ProfileError):
            Profile("t", (), points=(profiles_pkg.PointSetting("E", 8, 16, -5),))
        with self.assertRaises(ProfileError):
            Profile("t", (), points=("nope",))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
