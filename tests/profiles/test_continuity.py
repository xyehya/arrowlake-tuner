"""G-123: profiles/contents.py (profile -> intent-like rows, summary) and profiles/lastread.py (the atomic, trust-checked last-read file). Temp dirs only."""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from arltuner.profiles import CoreSetting, PointSetting, Profile, TurboTables
from arltuner.profiles.contents import intent_contents, profile_contents, summarize
from arltuner.profiles.lastread import LastReadError, LastReadStore
from arltuner.topology import arl_s_285k

P_RATIOS = (8, 20, 30, 48, 53, 55, 56, 57)
E_RATIOS = (8, 20, 30, 36, 40, 46)
TOPO = arl_s_285k()


def unit_of(cpu: int) -> str:
    core = TOPO.by_cpu(cpu)
    return f"P{core.index}" if core.cluster is None else f"EC{core.cluster}"


def contents(profile: Profile) -> dict:
    return profile_contents(profile, TOPO, unit_of, {"P": P_RATIOS, "E": E_RATIOS})


class Contents(unittest.TestCase):
    def test_point_numbers_become_ratios(self) -> None:
        got = contents(Profile("x", (), None, (PointSetting("P", 1, 5, -50), PointSetting("P", 1, 8, -10))))
        self.assertEqual(got["rows"], [{"target": "P1", "offset": {"kind": "points", "mv": {"53": -50, "57": -10}}, "v_target_v": None, "ratio": None}])
        self.assertIsNone(got["turbo"])

    def test_an_e_core_entry_is_its_cluster(self) -> None:
        got = contents(Profile("x", (), None, (PointSetting("E", 5, 6, -25), PointSetting("E", 6, 6, -25))))  # E cores 5 and 6 are both in cluster 1
        self.assertEqual([r["target"] for r in got["rows"]], ["EC1"])
        self.assertEqual(got["rows"][0]["offset"]["mv"], {"46": -25})

    def test_core_fields_and_the_curve_plus_points_rule(self) -> None:
        prof = Profile("x", (CoreSetting("P", 0, ratio=58, oc_voltage_mv=1300, offset_mv=-20), CoreSetting("P", 3, offset_mv=-15)), None,
                       (PointSetting("P", 0, 5, -50),), )
        got = {r["target"]: r for r in contents(prof)["rows"]}
        self.assertEqual((got["P0"]["ratio"], got["P0"]["v_target_v"]), (58, 1.3))
        self.assertEqual(got["P0"]["offset"]["mv"], {str(r): (-50 if r == 53 else -20) for r in P_RATIOS})
        self.assertEqual(got["P3"]["offset"], {"kind": "curve", "mv": -15})

    def test_turbo(self) -> None:
        prof = Profile("x", (CoreSetting("P", 0, offset_mv=-5),), TurboTables(p=(57, 57, 55, 55, 55, 55, 55, 55)))
        self.assertEqual(contents(prof)["turbo"], {"P": [57, 57, 55, 55, 55, 55, 55, 55]})

    def test_saved_intent_spreads_groups(self) -> None:
        got = intent_contents({"scope": "simple", "rows": [{"target": "E", "offset": {"kind": "curve", "mv": -30}, "v_target_v": None, "ratio": None}]})
        self.assertEqual([r["target"] for r in got["rows"]], ["EC0", "EC1", "EC2", "EC3"])

    def test_summary(self) -> None:
        self.assertEqual(summarize({"rows": [], "turbo": None}), {"units": 0, "points": 0, "deepest_offset_mv": None, "oc_ratios": 0, "v_targets": 0, "turbo": False})
        s = summarize({"rows": [{"target": "P0", "offset": {"kind": "points", "mv": {"53": -50, "55": -91}}, "v_target_v": 1.27, "ratio": 58},
                                {"target": "P1", "offset": {"kind": "curve", "mv": -5}, "v_target_v": None, "ratio": None}], "turbo": {"E": [1] * 8}})
        self.assertEqual(s, {"units": 2, "points": 2, "deepest_offset_mv": -91, "oc_ratios": 1, "v_targets": 1, "turbo": True})


class LastRead(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = LastReadStore(self.root / "var" / "state", euid=os.geteuid())
        self.rec = {"version": 1, "boot_id": "b", "at": 1.5, "applied_at": None, "words": {"0": ["0x00000039"]}, "result": {"complete": True}}

    def test_round_trip_is_private_and_leaves_no_temp_files(self) -> None:
        self.assertIsNone(self.store.load())
        self.store.save(self.rec)
        self.assertEqual(self.store.load(), self.rec)
        self.assertEqual(stat.S_IMODE(os.stat(self.store.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.store.directory).st_mode), 0o700)
        self.assertEqual(os.listdir(self.store.directory), ["last-read.json"])

    def test_overwrite_is_atomic_replace(self) -> None:
        self.store.save(self.rec)
        ino = os.stat(self.store.path).st_ino
        self.store.save({**self.rec, "at": 2.5})
        self.assertEqual(self.store.load()["at"], 2.5)
        self.assertNotEqual(os.stat(self.store.path).st_ino, ino)  # a new file renamed over the old one

    def test_symlink_target_is_untouched_and_never_read(self) -> None:
        self.store.save(self.rec)
        victim = self.root / "victim"
        victim.write_text("keep")
        os.unlink(self.store.path)
        os.symlink(victim, self.store.path)
        with self.assertRaises(LastReadError) as cm:
            self.store.load()
        self.assertEqual(cm.exception.code, "UNTRUSTED")
        self.store.save(self.rec)
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse(self.store.path.is_symlink())

    def test_untrusted_directory_and_records(self) -> None:
        self.store.save(self.rec)
        os.chmod(self.store.directory, 0o775)
        with self.assertRaises(LastReadError):
            self.store.load()
        with self.assertRaises(LastReadError):
            self.store.save(self.rec)
        os.chmod(self.store.directory, 0o700)
        self.store.path.write_text('{"version": 2}')
        with self.assertRaises(LastReadError):
            self.store.load()

    def test_too_big_is_refused_before_anything_is_written(self) -> None:
        with self.assertRaises(LastReadError) as cm:
            self.store.save({**self.rec, "result": {"x": "y" * 300_000}})
        self.assertEqual(cm.exception.code, "TOO_BIG")
        self.assertFalse(self.store.directory.exists())


if __name__ == "__main__":
    unittest.main()
