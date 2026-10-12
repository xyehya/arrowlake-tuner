"""G-129: the accepted-state writer (``arltuner.profiles.accepted``) and the profile serializer (``arltuner.profiles.serialize``). Temp dirs only."""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from arltuner.profiles import CoreSetting, PointSetting, Profile, ProfileError, TurboTables, load_profile
from arltuner.profiles.accepted import AcceptedError, AcceptedStore
from arltuner.profiles.serialize import dump_profile


class Serialize(unittest.TestCase):
    def test_every_shape_round_trips(self) -> None:
        profile = Profile(
            "mine-1.x",
            (CoreSetting("P", 0, ratio=57, oc_voltage_mv=1300, offset_mv=-25), CoreSetting("E", 8, offset_mv=-10), CoreSetting("P", 1, oc_voltage_mv=1250)),
            TurboTables(e=(49,) * 8, p=(57, 57, 55, 55, 55, 55, 55, 55)),
            (PointSetting("P", 3, 5, -60, 53), PointSetting("E", 8, 6, 0)),
        )
        text = dump_profile(profile, "Accepted today.\nsecond line")
        self.assertTrue(text.startswith("# Accepted today.\n# second line\n[meta]\n"))
        self.assertEqual(load_profile(text), profile)

    def test_turbo_only_and_points_only_profiles(self) -> None:
        for profile in (Profile("t", (), TurboTables(p=(57,) * 8)), Profile("p", (), None, (PointSetting("P", 0, 1, -5, 8),))):
            self.assertEqual(load_profile(dump_profile(profile)), profile)

    def test_a_name_that_needs_escaping_is_refused(self) -> None:
        for name in ('a"b', "a b", "a\nb", "", "é"):
            with self.subTest(name=name), self.assertRaises(ProfileError):
                dump_profile(Profile(name, (CoreSetting("P", 0, offset_mv=-1),), None, ()))


class Store(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.etc = Path(tmp.name) / "etc"
        self.etc.mkdir(mode=0o755)
        self.etc.chmod(0o755)
        self.store = AcceptedStore(self.etc / "accepted", euid=os.geteuid())

    def test_creates_the_tree_with_the_modes_the_helper_trusts(self) -> None:
        self.store.replace("mine", "[meta]\n", "0  39 8\n")
        acc = self.etc / "accepted"
        self.assertEqual((acc / "CURRENT").read_text(), "profile = profiles/mine.toml\n")
        self.assertEqual((acc / "profiles" / "mine.toml").read_text(), "[meta]\n")
        for p in [acc, acc / "profiles", *acc.rglob("*")]:
            mode = stat.S_IMODE(p.lstat().st_mode)
            self.assertEqual(mode, 0o755 if p.is_dir() else 0o644, p)
            self.assertEqual(p.lstat().st_uid, os.geteuid())
        self.assertEqual(sorted(p.name for p in self.etc.iterdir()), ["accepted"])

    def test_the_fingerprint_is_written_as_a_fourth_file_when_given(self) -> None:
        self.store.replace("mine", "[meta]\n", "0  39 8\n", fingerprint='{"schema": 1}\n')
        acc = self.etc / "accepted"
        self.assertEqual((acc / "fingerprint.json").read_text(), '{"schema": 1}\n')
        self.assertEqual(stat.S_IMODE((acc / "fingerprint.json").stat().st_mode), 0o644)
        self.store.replace("two", "[meta]\n", "0  39 8\n")  # replaced as a whole: the old fingerprint does not survive a state without one
        self.assertFalse((acc / "fingerprint.json").exists())

    def test_the_umask_does_not_decide_the_modes(self) -> None:
        old = os.umask(0o077)
        try:
            self.store.replace("mine", "x\n", "y\n")
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE((self.etc / "accepted" / "CURRENT").stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((self.etc / "accepted").stat().st_mode), 0o755)

    def test_replaces_everything_and_leaves_no_staging_paths(self) -> None:
        self.store.replace("one", "1\n", "1\n")
        (self.etc / "accepted" / "junk").write_text("x")
        self.store.replace("two", "2\n", "2\n")
        self.assertEqual(sorted(p.relative_to(self.etc / "accepted").as_posix() for p in (self.etc / "accepted").rglob("*")), ["CURRENT", "EXPECTED-words.txt", "profiles", "profiles/two.toml"])
        self.assertEqual(sorted(p.name for p in self.etc.iterdir()), ["accepted"])

    def test_a_failure_leaves_the_old_state_in_place(self) -> None:
        self.store.replace("one", "1\n", "1\n")
        (self.etc / f".accepted.new.{os.getpid()}").write_text("a file is in the way of nothing: it is removed first")
        self.store.replace("two", "2\n", "2\n")
        self.assertIn("two", (self.etc / "accepted" / "CURRENT").read_text())
        broken = AcceptedStore(self.etc / "accepted", euid=os.geteuid())
        original = os.rename

        def failing(src, dst, *a, **k):
            if str(dst) == str(self.etc / "accepted") and ".accepted.new" in str(src):
                raise OSError(28, "No space left on device")
            return original(src, dst, *a, **k)

        os.rename = failing  # type: ignore[assignment]
        try:
            with self.assertRaises(AcceptedError):
                broken.replace("three", "3\n", "3\n")
        finally:
            os.rename = original  # type: ignore[assignment]
        self.assertIn("two", (self.etc / "accepted" / "CURRENT").read_text())
        self.assertEqual(sorted(p.name for p in self.etc.iterdir()), ["accepted"])

    def test_bad_names_and_untrusted_parents(self) -> None:
        for name in ("", "..", "a/b", "a..b", ".hidden", "-x", "x" * 65, "a b"):
            with self.subTest(name=name), self.assertRaises(AcceptedError) as cm:
                self.store.replace(name, "x\n", "y\n")
            self.assertEqual(cm.exception.code, "BAD_NAME")
        self.etc.chmod(0o775)
        with self.assertRaises(AcceptedError) as cm:
            self.store.replace("ok", "x\n", "y\n")
        self.assertEqual(cm.exception.code, "UNTRUSTED")
        self.etc.chmod(0o755)
        with self.assertRaises(AcceptedError) as cm:
            AcceptedStore(self.etc / "accepted", euid=os.geteuid() + 1).replace("ok", "x\n", "y\n")
        self.assertEqual(cm.exception.code, "UNTRUSTED")
        with self.assertRaises(AcceptedError) as cm:
            AcceptedStore(self.etc / "missing" / "accepted", euid=os.geteuid()).replace("ok", "x\n", "y\n")
        self.assertEqual(cm.exception.code, "IO")
        self.assertFalse((self.etc / "accepted").exists())
