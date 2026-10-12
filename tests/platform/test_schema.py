"""Platform file loader/validator tests (G-101, G-128). Reads only the repo's platforms/ file and tmp files."""
from __future__ import annotations

import ast
import copy
import tempfile
import tomllib
import unittest
from pathlib import Path

from arltuner.msr import SimConfig
from arltuner.msr.sim import PLAIN_MSR_DEFAULTS, POINT_COUNTS_ARL_S, POINT_RATIOS_ARL_S, RING_POINT_RATIOS_ARL_S
from arltuner.platform import PlatformError, default_platform, load_platform, parse_platform
from arltuner.platform.schema import default_platform_path
from arltuner.topology import arl_s_285k

REPO = Path(__file__).resolve().parents[2]


def _doc() -> dict:
    with default_platform_path().open("rb") as fh:
        return tomllib.load(fh)


class DefaultPlatformTests(unittest.TestCase):
    def test_file_loads_as_a_family_file(self):
        p = default_platform()
        self.assertEqual((p.id, p.name), ("arl-s", "Intel Core Ultra (Arrow Lake-S)"))
        self.assertEqual((p.cpuid.vendor, p.cpuid.family, p.cpuid.model, p.cpuid.steppings), ("GenuineIntel", 6, 198, (2,)))
        self.assertEqual(default_platform_path().name, "arl-s.toml")

    def test_the_file_holds_no_sku_specific_tables(self):
        # D-036: no topology, favored cores, fused words, V/F point ratios or turbo defaults in the platform file
        self.assertEqual(set(_doc()), {"schema", "platform", "cpuid", "sensors", "boards"})
        self.assertFalse(hasattr(default_platform(), "topology"))
        self.assertFalse(hasattr(default_platform(), "fused_point"))

    def test_the_simulated_reference_cpu_keeps_its_fixture_tables(self):
        self.assertEqual([POINT_RATIOS_ARL_S[("P", n)] for n in range(1, 9)], [8, 20, 30, 48, 53, 55, 56, 57])
        self.assertEqual([POINT_RATIOS_ARL_S[("E", n)] for n in range(1, 7)], [8, 15, 23, 30, 40, 46])
        self.assertEqual(list(RING_POINT_RATIOS_ARL_S.values()), [8, 14, 20, 28, 34, 37, 38, 39])
        self.assertEqual(PLAIN_MSR_DEFAULTS[0x1AD], 0x3737373737373939)
        self.assertEqual(PLAIN_MSR_DEFAULTS[0x1AE], 0x0807060504030201)
        self.assertEqual(PLAIN_MSR_DEFAULTS[0x650], 0x3030303030303030)
        self.assertEqual(PLAIN_MSR_DEFAULTS[0x651], 0x100C080604030201)
        self.assertEqual(POINT_COUNTS_ARL_S, {"E": 6, "P": 8})
        self.assertEqual(SimConfig.measured().topology, arl_s_285k())

    def test_board_table_keyed_by_dmi_board_name(self):
        s = default_platform().sensors_for("Z890 Steel Legend WiFi")
        self.assertEqual((s.temp_hwmon_name, s.vcore_hwmon_vendor, s.vcore_hwmon_name, s.vcore_hwmon_label), ("coretemp", "Nuvoton", "nct6686", "VIN16"))

    def test_unknown_board_has_temperatures_but_no_vcore_never_a_guess(self):
        p = default_platform()
        for board in ("Some Other Board", "", None, "z890 steel legend wifi"):
            with self.subTest(board=board):
                s = p.sensors_for(board)
                self.assertEqual((s.temp_hwmon_name, s.vcore_hwmon_name, s.vcore_hwmon_label), ("coretemp", "", ""))


class ValidationTests(unittest.TestCase):
    def bad(self, mutate, pattern):
        d = copy.deepcopy(_doc())
        mutate(d)
        with self.assertRaisesRegex(PlatformError, pattern):
            parse_platform(d)

    def test_schema_version(self):
        self.bad(lambda d: d.update(schema=1), "schema")

    def test_the_old_sku_tables_are_refused(self):
        for key in ("topology", "vf_points", "fused_point", "turbo"):
            with self.subTest(key=key):
                self.bad(lambda d, key=key: d.update({key: {}}), "unknown key")

    def test_missing_table(self):
        self.bad(lambda d: d.pop("cpuid"), r"\[cpuid\]")

    def test_missing_key(self):
        self.bad(lambda d: d["cpuid"].pop("vendor"), "vendor")

    def test_unknown_key(self):
        self.bad(lambda d: d["cpuid"].update(extra=1), "unknown key")

    def test_steppings_must_be_a_list_of_distinct_ints_in_range(self):
        self.bad(lambda d: d["cpuid"].update(steppings=2), "steppings")
        self.bad(lambda d: d["cpuid"].update(steppings=[]), "steppings")
        self.bad(lambda d: d["cpuid"].update(steppings=[2, 2]), "steppings")
        self.bad(lambda d: d["cpuid"].update(steppings=[99]), "steppings")
        d = _doc()
        d["cpuid"]["steppings"] = [1, 2, 3]
        self.assertEqual(parse_platform(d).cpuid.steppings, (1, 2, 3))

    def test_range(self):
        self.bad(lambda d: d["cpuid"].update(model=999), "model")

    def test_empty_temp_sensor_name_refused_but_empty_vcore_name_and_label_allowed(self):
        self.bad(lambda d: d["sensors"].update(temp_hwmon_name=""), "temp_hwmon_name")
        d = _doc()
        d["boards"]["Z890 Steel Legend WiFi"]["vcore_hwmon_name"] = ""
        d["boards"]["Z890 Steel Legend WiFi"]["vcore_hwmon_label"] = ""
        s = parse_platform(d).sensors_for("Z890 Steel Legend WiFi")
        self.assertEqual((s.vcore_hwmon_name, s.vcore_hwmon_label), ("", ""))

    def test_board_entries_are_validated(self):
        self.bad(lambda d: d["boards"]["Z890 Steel Legend WiFi"].update(vcore_hwmon_label=16), "vcore_hwmon_label")
        self.bad(lambda d: d["boards"]["Z890 Steel Legend WiFi"].pop("vcore_hwmon_label"), "vcore_hwmon_label")
        self.bad(lambda d: d["boards"]["Z890 Steel Legend WiFi"].update(vcore_in="in0"), "vcore_in")
        self.bad(lambda d: d.update(boards=[]), "boards")
        self.bad(lambda d: d["boards"].update(Broken="x"), "must be a table")

    def test_a_platform_without_boards_is_valid(self):
        d = _doc()
        del d["boards"]
        self.assertEqual(parse_platform(d).sensors_for("Z890 Steel Legend WiFi").vcore_hwmon_name, "")


class LoadErrorTests(unittest.TestCase):
    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaisesRegex(PlatformError, "cannot read"):
                load_platform(Path(t) / "nope.toml")

    def test_invalid_toml(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "bad.toml"
            p.write_text("schema = [", encoding="utf-8")
            with self.assertRaisesRegex(PlatformError, "invalid TOML"):
                load_platform(p)

    def test_explicit_path_loads(self):
        self.assertEqual(load_platform(default_platform_path()), default_platform())


class FixtureConfinementTests(unittest.TestCase):
    """D-036: the simulated reference CPU's tables are a fixture. ``msr/measured_baseline.py`` may be imported by ``msr/sim.py`` and by tests only, and no 285K /
    "this machine" lore is left outside the simulator and the fixtures."""

    SRC = REPO / "src" / "arltuner"
    FIXTURES = {"msr/sim.py", "msr/measured_baseline.py", "topology/reference.py"}

    def imports_of(self, path: Path) -> set[str]:
        out: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                out.add(node.module or "")
                out.update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                out.update(a.name for a in node.names)
        return out

    def test_measured_baseline_is_imported_only_by_the_simulator(self):
        users = []
        for path in sorted(self.SRC.rglob("*.py")):
            if path.name == "measured_baseline.py":
                continue
            if any(name == "measured_baseline" or name.endswith(".measured_baseline") for name in self.imports_of(path)):
                users.append(path.relative_to(self.SRC).as_posix())
        self.assertEqual(users, ["msr/sim.py"])

    def test_no_reference_machine_lore_outside_the_fixtures(self):
        bad = []
        for path in sorted(self.SRC.rglob("*.py")):
            rel = path.relative_to(self.SRC).as_posix()
            if rel in self.FIXTURES:
                continue
            text = path.read_text()
            for needle in ("285K", "this machine"):
                if needle in text:
                    bad.append(f"{rel}: {needle}")
        self.assertEqual(bad, [])

    def test_the_old_platform_tables_are_gone_from_the_runtime_code(self):
        for path in sorted(self.SRC.rglob("*.py")):
            text = path.read_text()
            for old in ("platform.fused_point", ".turbo_defaults", ".p_point_ratios", ".e_point_ratios", "topology.favored_p", "QUICK_RATIOS", "_PLATFORM_TURBO", "_P_FAVORED_STOCK"):
                self.assertNotIn(old, text, f"{path.name} still uses {old}")


if __name__ == "__main__":
    unittest.main()
