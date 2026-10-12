"""G-112: the root store of saved intents (profiles/store.py): names, never overwrite, symlink/permission refusals, bounded listing. Temp directories only."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from arltuner.profiles.store import MAX_PROFILE_BYTES, MAX_PROFILES, ProfileStore, ProfileStoreError

INTENT = {"scope": "unit", "rows": [{"target": "P0", "offset": {"kind": "curve", "mv": -10}, "v_target_v": None, "ratio": None}]}


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dir = self.root / "state" / "profiles"
        self.store = ProfileStore(self.dir, euid=os.geteuid(), wall_clock=lambda: 1_700_000_000.0)

    def code(self, fn, *args) -> str:  # type: ignore[no-untyped-def]
        with self.assertRaises(ProfileStoreError) as cm:
            fn(*args)
        return cm.exception.code


class Names(StoreCase):
    def test_the_contract_pattern(self) -> None:
        for good in ("a", "p-step7", "x.y_z-1", "0" * 64):
            self.assertEqual(self.store.save(good, INTENT)["name"], good)
        for bad in ("", "A", "a b", "a/b", "x" * 65, "é", "a\n", "a.json/..", "\x00"):
            with self.subTest(bad=bad):
                self.assertEqual(self.code(self.store.save, bad, INTENT), "BAD_NAME")
                self.assertEqual(self.code(self.store.load, bad), "BAD_NAME")


class Save(StoreCase):
    def test_creates_a_private_directory_and_a_0600_file(self) -> None:
        rec = self.store.save("one", INTENT)
        self.assertEqual(rec["created"], "2023-11-14T22:13:20Z")
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / "one.json").st_mode), 0o600)
        self.assertEqual(json.loads((self.dir / "one.json").read_text())["intent"], INTENT)

    def test_never_overwrites(self) -> None:
        self.store.save("one", INTENT)
        other = {**INTENT, "scope": "simple"}
        self.assertEqual(self.code(self.store.save, "one", other), "EXISTS")
        self.assertEqual(self.store.load("one")["intent"], INTENT)

    def test_a_planted_symlink_is_not_followed(self) -> None:
        self.store.save("seed", INTENT)
        victim = self.root / "victim"
        victim.write_text("keep")
        os.symlink(victim, self.dir / "link.json")
        self.assertEqual(self.code(self.store.save, "link", INTENT), "EXISTS")
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual(self.code(self.store.load, "link"), "UNTRUSTED")

    def test_a_dangling_symlink_is_not_created_through(self) -> None:
        self.store.save("seed", INTENT)
        os.symlink(self.root / "nowhere", self.dir / "dangling.json")
        self.assertEqual(self.code(self.store.save, "dangling", INTENT), "EXISTS")
        self.assertFalse((self.root / "nowhere").exists())

    def test_oversize_is_refused(self) -> None:
        big = {"scope": "unit", "rows": [{"pad": "x" * MAX_PROFILE_BYTES}]}
        self.assertEqual(self.code(self.store.save, "big", big), "TOO_BIG")
        self.assertFalse((self.dir / "big.json").exists())

    def test_the_profile_count_is_capped_with_a_clear_error(self) -> None:  # G-142
        self.store.save("seed", INTENT)
        for i in range(MAX_PROFILES - 1):
            (self.dir / f"p{i}.json").write_text("{}")
        self.store.list()  # a full directory still lists (bounded) and loads
        with self.assertRaises(ProfileStoreError) as cm:
            self.store.save("one-too-many", INTENT)
        self.assertEqual(cm.exception.code, "TOO_BIG")  # the helper maps TOO_BIG to BAD_REQUEST (ops_tune)
        self.assertIn(str(MAX_PROFILES), cm.exception.message)
        self.assertFalse((self.dir / "one-too-many.json").exists())
        (self.dir / "p0.json").unlink()
        self.assertEqual(self.store.save("fits-now", INTENT)["name"], "fits-now")

    def test_a_group_writable_directory_is_refused(self) -> None:
        self.store.save("seed", INTENT)
        os.chmod(self.dir, 0o770)
        self.assertEqual(self.code(self.store.save, "two", INTENT), "UNTRUSTED")
        with self.assertRaises(ProfileStoreError):
            self.store.list()  # an untrusted directory is a store error, never a silent empty list and never an unrelated exception

    def test_a_symlinked_directory_is_refused(self) -> None:
        real = self.root / "real"
        real.mkdir(mode=0o700)
        self.dir.parent.mkdir(parents=True)
        os.symlink(real, self.dir)
        self.assertEqual(self.code(self.store.save, "x", INTENT), "UNTRUSTED")
        self.assertEqual(list(real.iterdir()), [])

    def test_a_foreign_owner_is_refused(self) -> None:
        self.store.save("seed", INTENT)
        foreign = ProfileStore(self.dir, euid=os.geteuid() + 1)
        self.assertEqual(self.code(foreign.save, "y", INTENT), "UNTRUSTED")
        self.assertEqual(self.code(foreign.load, "seed"), "UNTRUSTED")


class LoadAndList(StoreCase):
    def test_load_missing(self) -> None:
        self.assertEqual(self.code(self.store.load, "nope"), "NOT_FOUND")
        self.store.save("seed", INTENT)
        self.assertEqual(self.code(self.store.load, "nope"), "NOT_FOUND")

    def test_list_is_sorted_and_skips_foreign_files(self) -> None:
        for name in ("b", "a", "c"):
            self.store.save(name, INTENT)
        (self.dir / "junk.txt").write_text("x")
        (self.dir / "Bad Name.json").write_text("{}")
        (self.dir / "mismatch.json").write_text(json.dumps({"name": "other", "intent": INTENT}))
        (self.dir / "notjson.json").write_text("{{{")
        os.chmod(self.dir / "c.json", 0o666)  # group/other writable: not a trusted profile
        listed = self.store.list()
        self.assertEqual([p["name"] for p in listed], ["a", "b"])
        self.assertTrue(all(p["source"] == "saved" and p["created"] == "2023-11-14T22:13:20Z" for p in listed))

    def test_list_without_a_directory_is_empty(self) -> None:
        self.assertEqual(self.store.list(), [])

    def test_a_corrupt_record_is_untrusted_not_a_crash(self) -> None:
        self.store.save("seed", INTENT)
        (self.dir / "bad.json").write_text(json.dumps({"name": "bad", "intent": "no"}))
        self.assertEqual(self.code(self.store.load, "bad"), "UNTRUSTED")
        (self.dir / "huge.json").write_text(" " * (MAX_PROFILE_BYTES + 10))
        self.assertEqual(self.code(self.store.load, "huge"), "UNTRUSTED")


if __name__ == "__main__":
    unittest.main()
