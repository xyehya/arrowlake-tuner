"""Platform file loader/validator tests (G-101). Reads only the repo's platforms/ file and tmp files."""
from __future__ import annotations

import copy
import tempfile
import tomllib
import unittest
from pathlib import Path

from arltuner.msr import FUSED_POINT, SimConfig, measured_baseline
from arltuner.msr.sim import PLAIN_MSR_DEFAULTS, POINT_RATIOS_ARL_S, RING_POINT_RATIOS_ARL_S
from arltuner.platform import PlatformError, default_platform, load_platform, parse_platform
from arltuner.platform.schema import default_platform_path
from arltuner.topology import arl_s_285k

# The literals the code carried before G-101 (no-behaviour-change pin).
_OLD_FUSED = {
    0: 0x5B439, 1: 0x53839, 2: 0x57439, 3: 0x59439, 4: 0x54439, 5: 0x56C39, 6: 0x56039, 7: 0x58039,
    **{c: 0x4A02E for c in range(8, 12)},
    **{c: 0x48C2E for c in range(12, 24)},
}


def _doc() -> dict:
    with default_platform_path().open("rb") as fh:
        return tomllib.load(fh)


class DefaultPlatformTests(unittest.TestCase):
    def test_file_loads(self):
        p = default_platform()
        self.assertEqual(p.id, "arl-s-285k")
        self.assertEqual(p.cpuid.signature, 0xC0662)
        self.assertEqual(p.topology.cpu_count, 24)
        self.assertEqual(p.sensors.temp_hwmon_name, "coretemp")

    def test_values_unchanged_from_old_constants(self):
        self.assertEqual(dict(FUSED_POINT), _OLD_FUSED)
        self.assertEqual(measured_baseline.CPU_COUNT, 24)
        self.assertEqual(measured_baseline.P_CPUS, tuple(range(8)))
        self.assertEqual(measured_baseline.E_CPUS, tuple(range(8, 24)))
        self.assertEqual([measured_baseline.LP_OF_CPU[c] for c in range(24)], [0, 1, 10, 11, 12, 13, 22, 23, 2, 3, 4, 5, 6, 7, 8, 9, 14, 15, 16, 17, 18, 19, 20, 21])
        self.assertEqual([POINT_RATIOS_ARL_S[("P", n)] for n in range(1, 9)], [8, 20, 30, 48, 53, 55, 56, 57])
        self.assertEqual([POINT_RATIOS_ARL_S[("E", n)] for n in range(1, 7)], [8, 15, 23, 30, 40, 46])
        self.assertEqual(list(RING_POINT_RATIOS_ARL_S.values()), [8, 14, 20, 28, 34, 37, 38, 39])
        self.assertEqual(PLAIN_MSR_DEFAULTS[0x1AD], 0x3737373737373939)
        self.assertEqual(PLAIN_MSR_DEFAULTS[0x1AE], 0x0807060504030201)
        self.assertEqual(PLAIN_MSR_DEFAULTS[0x650], 0x3030303030303030)
        self.assertEqual(PLAIN_MSR_DEFAULTS[0x651], 0x100C080604030201)
        self.assertEqual(SimConfig.measured().topology, arl_s_285k())

    def test_topology_from_file(self):
        topo = arl_s_285k()
        self.assertEqual([c.cpu for c in topo.cores if c.favored], [0, 3])
        self.assertEqual(topo.cluster_cpus(3), (20, 21, 22, 23))

    def test_signature_consistent(self):
        d = _doc()
        d["cpuid"]["signature"] = 0xC0663
        with self.assertRaisesRegex(PlatformError, "signature"):
            parse_platform(d)


class ValidationTests(unittest.TestCase):
    def bad(self, mutate, pattern):
        d = copy.deepcopy(_doc())
        mutate(d)
        with self.assertRaisesRegex(PlatformError, pattern):
            parse_platform(d)

    def test_schema_version(self):
        self.bad(lambda d: d.update(schema=2), "schema")

    def test_missing_table(self):
        self.bad(lambda d: d.pop("turbo"), r"\[turbo\]")

    def test_missing_key(self):
        self.bad(lambda d: d["cpuid"].pop("vendor"), "vendor")

    def test_unknown_key(self):
        self.bad(lambda d: d["cpuid"].update(extra=1), "unknown key")

    def test_wrong_type(self):
        self.bad(lambda d: d["topology"].update(p_count="8"), "p_count")

    def test_range(self):
        self.bad(lambda d: d["cpuid"].update(stepping=99), "stepping")

    def test_ratios_not_ascending(self):
        self.bad(lambda d: d["vf_points"].update(p=[8, 30, 20, 48, 53, 55, 56, 57]), "ascending")

    def test_fused_words_not_distinct(self):
        def m(d):
            d["fused_point"]["p"][1] = d["fused_point"]["p"][0]
        self.bad(m, "distinct")

    def test_fused_word_wrong_length(self):
        self.bad(lambda d: d["fused_point"].update(p=[0x5B439]), "entries")

    def test_fused_word_ratio_mismatch(self):
        self.bad(lambda d: d["fused_point"].update(e_first_cluster=0x4A02F), "top point")

    def test_lp_map_length(self):
        self.bad(lambda d: d["topology"].update(lp_p=[0, 1]), "lengths")

    def test_lp_map_duplicates(self):
        def m(d):
            d["topology"]["lp_e"][0] = 0
        self.bad(m, "distinct")

    def test_favored_out_of_range(self):
        self.bad(lambda d: d["topology"].update(favored_p=[0, 9]), "favored_p")

    def test_turbo_missing_msr(self):
        self.bad(lambda d: d["turbo"].pop("msr_0x650"), "msr_0x650")

    def test_empty_sensor_name_allowed_only_for_vcore_name(self):
        self.bad(lambda d: d["sensors"].update(temp_hwmon_name=""), "temp_hwmon_name")
        d = _doc()
        d["sensors"]["vcore_hwmon_name"] = ""
        parse_platform(d)


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


if __name__ == "__main__":
    unittest.main()
