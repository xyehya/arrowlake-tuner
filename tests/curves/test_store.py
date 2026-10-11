"""The curve store and record helpers (contract 9): trust rules of contract 8, atomic writes, limits, the one-baseline rule, the 64 KiB fit."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any

from arltuner.curves import MAX_RECORDS, CurveStore, StoreError, fit_record, is_curve_id, new_curve_id, summary, validate_name


def record(n: int = 0, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": new_curve_id(1_800_000_000 + n, __import__("random").Random(n)), "name": f"curve {n}", "created": "2027-01-15T08:00:00Z", "boot_id": None, "mode": "quick",
        "complete": True, "kernel": "6.x", "tuning_state": {"kind": "unknown"}, "temps": {"min_c": 40.0, "max_c": 60.0}, "baseline": False,
        "units": {"P0": {"cpus": [0], "points": [{"ratio": 8, "mhz": 800, "vid_v": 0.7, "vid_min": 0.69, "vid_max": 0.71, "kept": 10, "total": 10, "status": "ok"}]}},
    }
    base.update(kw)
    return base


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "curves"
        self.store = CurveStore(self.root)

    def code(self, fn, *a, **k) -> tuple[str, dict | None]:
        with self.assertRaises(StoreError) as cm:
            fn(*a, **k)
        return cm.exception.code, cm.exception.detail


class RoundTripTests(StoreCase):
    def test_save_get_list_newest_first(self) -> None:
        a, b = self.store.save(record(1)), self.store.save(record(2))
        self.assertEqual(self.store.get(a["id"]), a)
        self.assertEqual([r["id"] for r in self.store.summaries()], [b["id"], a["id"]])
        self.assertEqual(self.store.summaries()[0], summary(b))

    def test_directory_and_files_are_private_and_nothing_is_left_behind(self) -> None:
        saved = self.store.save(record(1))
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / f"{saved['id']}.json").st_mode), 0o600)
        self.assertEqual(os.listdir(self.root), [f"{saved['id']}.json"])  # the temp file was renamed over, not copied
        self.store.update(saved["id"], name="again")
        self.assertEqual(len(os.listdir(self.root)), 1)

    def test_an_id_is_assigned_and_well_formed(self) -> None:
        rec = record(1)
        del rec["id"]
        saved = self.store.save(rec)
        self.assertTrue(is_curve_id(saved["id"]), saved["id"])
        self.assertRegex(saved["id"], r"^[0-9]{8}T[0-9]{6}Z-[a-z0-9]{4}$")

    def test_an_existing_id_is_never_overwritten_by_save(self) -> None:
        saved = self.store.save(record(1))
        self.assertEqual(self.code(self.store.save, record(1))[1], {"check": "EXISTS"})
        self.assertEqual(self.store.get(saved["id"])["name"], "curve 1")

    def test_a_crash_between_temp_and_rename_leaves_no_half_record(self) -> None:
        saved = self.store.save(record(1))
        (self.root / f".{saved['id']}.deadbeef.tmp").write_text("{half")
        self.assertEqual([r["id"] for r in self.store.summaries()], [saved["id"]])

    def test_ids_are_validated_before_any_path_is_built(self) -> None:
        for bad in ("../x", "20260101T000000Z-ABCD", "x", "", "20260101T000000Z-abcd/../../etc", 5):
            self.assertEqual(self.code(self.store.get, bad)[0], "BAD_REQUEST", bad)
            self.assertEqual(self.code(self.store.delete, bad)[0], "BAD_REQUEST", bad)
        self.assertEqual(self.code(self.store.get, "20260101T000000Z-abcd")[1], {"check": "NO_SUCH_CURVE"})

    def test_list_before_the_directory_exists_is_empty_and_creates_nothing(self) -> None:
        self.assertEqual(self.store.summaries(), [])
        self.assertFalse(self.root.exists())


class RulesTests(StoreCase):
    def test_one_baseline_at_most(self) -> None:
        a, b, c = (self.store.save(record(n)) for n in (1, 2, 3))
        self.store.update(a["id"], baseline=True)
        self.store.update(b["id"], baseline=True)
        self.assertEqual({r["id"] for r in self.store.summaries() if r["baseline"]}, {b["id"]})
        self.store.update(b["id"], baseline=False)
        self.assertEqual([r for r in self.store.summaries() if r["baseline"]], [])
        self.store.save(record(4, baseline=True))
        self.store.save(record(5, baseline=True))
        self.assertEqual(len([r for r in self.store.summaries() if r["baseline"]]), 1)

    def test_rename_validates(self) -> None:
        a = self.store.save(record(1))
        self.assertEqual(self.store.update(a["id"], name="  nice  ")["name"], "nice")
        for bad in ("", "   ", "x" * 65, "tab\there", 5):
            self.assertEqual(self.code(self.store.update, a["id"], name=bad)[0], "BAD_REQUEST", bad)
        self.assertEqual(self.code(self.store.update, a["id"], baseline="yes")[0], "BAD_REQUEST")
        self.assertEqual(self.store.get(a["id"])["name"], "nice")

    def test_at_most_200_records(self) -> None:
        for n in range(MAX_RECORDS):
            self.store.save(record(n))
        code, detail = self.code(self.store.save, record(MAX_RECORDS))
        self.assertEqual((code, detail), ("REFUSED", {"check": "CURVE_LIMIT"}))
        victim = self.store.summaries()[0]["id"]
        self.store.delete(victim)
        self.store.save(record(MAX_RECORDS))  # room again

    def test_delete(self) -> None:
        a = self.store.save(record(1))
        self.store.delete(a["id"])
        self.assertEqual(self.store.summaries(), [])
        self.assertEqual(self.code(self.store.delete, a["id"])[1], {"check": "NO_SUCH_CURVE"})


class TrustTests(StoreCase):
    def test_a_symlinked_record_is_refused_and_never_followed(self) -> None:
        saved = self.store.save(record(1))
        victim = self.root.parent / "victim.json"
        victim.write_text(json.dumps(record(9)))
        link = self.root / "20270101T000000Z-link.json"
        os.symlink(victim, link)
        self.assertEqual(self.code(self.store.get, "20270101T000000Z-link")[1], {"check": "TRUST_PATH"})
        self.assertEqual([r["id"] for r in self.store.summaries()], [saved["id"]])  # skipped in a listing, and named
        self.assertTrue(any("20270101T000000Z-link" in p for p in self.store.problems))
        self.assertEqual(victim.read_text(), json.dumps(record(9)))

    def test_a_record_owned_by_somebody_else_is_refused(self) -> None:
        import types

        saved = self.store.save(record(1))

        def foreign_fstat(fd: int) -> Any:
            st = os.fstat(fd)
            return types.SimpleNamespace(st_mode=st.st_mode, st_nlink=st.st_nlink, st_uid=st.st_uid + 1)

        foreign = CurveStore(self.root, fstat_fn=foreign_fstat)  # the directory is ours, the files claim another owner
        self.assertEqual(self.code(foreign.get, saved["id"])[1], {"check": "TRUST_PATH"})
        self.assertEqual(self.code(foreign.save, record(2))[1], {"check": "TRUST_PATH"})  # and nothing is written through a foreign temp file either
        self.assertEqual(self.store.get(saved["id"])["id"], saved["id"])
        # a store that is not the directory's owner refuses the directory itself
        self.assertEqual(self.code(CurveStore(self.root, euid=os.geteuid() + 1).summaries)[0], "REFUSED")

    def test_a_group_or_world_writable_record_is_refused(self) -> None:
        saved = self.store.save(record(1))
        path = self.root / f"{saved['id']}.json"
        for mode in (0o620, 0o602, 0o666):
            os.chmod(path, mode)
            self.assertEqual(self.code(self.store.get, saved["id"])[1], {"check": "TRUST_PATH"}, oct(mode))
        os.chmod(path, 0o600)
        self.assertEqual(self.store.get(saved["id"])["id"], saved["id"])

    def test_a_hard_linked_record_is_refused(self) -> None:
        saved = self.store.save(record(1))
        os.link(self.root / f"{saved['id']}.json", self.root.parent / "alias.json")
        self.assertEqual(self.code(self.store.get, saved["id"])[1], {"check": "TRUST_PATH"})

    def test_a_directory_that_is_a_symlink_or_writable_by_others_is_refused(self) -> None:
        self.store.save(record(1))
        moved = self.root.parent / "real"
        os.rename(self.root, moved)
        os.symlink(moved, self.root)
        self.assertEqual(self.code(self.store.summaries)[1], {"check": "TRUST_PATH"})
        self.assertEqual(self.code(self.store.save, record(2))[1], {"check": "TRUST_PATH"})
        os.unlink(self.root)
        os.rename(moved, self.root)
        os.chmod(self.root, 0o777)
        self.assertEqual(self.code(self.store.summaries)[1], {"check": "TRUST_PATH"})

    def test_a_planted_fifo_or_directory_named_like_a_record_does_not_hang_or_break_listing(self) -> None:
        saved = self.store.save(record(1))
        os.mkfifo(self.root / "20270101T000000Z-fifo.json")
        os.mkdir(self.root / "20270101T000000Z-dirx.json")
        self.assertEqual([r["id"] for r in self.store.summaries()], [saved["id"]])
        self.assertEqual(len(self.store.problems), 2)

    def test_unreadable_content_is_reported_not_trusted(self) -> None:
        saved = self.store.save(record(1))
        (self.root / "20270101T000000Z-junk.json").write_text("not json")
        wrong_id = record(2)
        (self.root / "20270101T000000Z-oops.json").write_text(json.dumps(wrong_id))  # the id inside does not match the file name
        self.assertEqual([r["id"] for r in self.store.summaries()], [saved["id"]])
        self.assertEqual(self.code(self.store.get, "20270101T000000Z-junk")[0], "INTERNAL")
        self.assertEqual(self.code(self.store.get, "20270101T000000Z-oops")[0], "INTERNAL")

    def test_enforce_trust_checks_the_whole_path_chain(self) -> None:
        self.store.save(record(1))
        strict = CurveStore(self.root, enforce_trust=True, lstat_fn=lambda p: os.lstat(p))
        # the temp directory is owned by this (non-root) user, so a root-ownership chain check must refuse it
        if os.geteuid() != 0:
            self.assertEqual(self.code(strict.summaries)[1], {"check": "TRUST_PATH"})


class RecordHelperTests(unittest.TestCase):
    def test_validate_name(self) -> None:
        self.assertEqual(validate_name(" a b "), "a b")
        self.assertEqual(validate_name("é" * 64), "é" * 64)
        for bad in ("", " ", "x" * 65, "a\x00b", None, 3, "a\nb"):
            with self.assertRaises(ValueError):
                validate_name(bad)

    def test_ids(self) -> None:
        self.assertTrue(is_curve_id("20261011T120000Z-ab12"))
        for bad in ("20261011T120000Z-AB12", "20261011T120000Z-ab1", "2026-10-11", None, 12, "20261011T120000Z-ab12\n"):
            self.assertFalse(is_curve_id(bad), bad)
        self.assertTrue(re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[a-z0-9]{4}", new_curve_id()))

    def test_fit_record_cuts_by_whole_units_and_says_so(self) -> None:
        big = record(1)
        big["units"] = {u: {"cpus": [0], "points": [{"ratio": r, "mhz": r * 100, "vid_v": 1.0, "vid_min": 0.9, "vid_max": 1.1, "kept": 10, "total": 10, "status": "ok"}
                                                      for r in range(8, 58)]} for u in ("P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "EC0", "EC1", "EC2", "EC3")}
        self.assertEqual(fit_record(big, 10**7), big)
        fit = fit_record(big, 20_000)
        self.assertLessEqual(len(json.dumps(fit, separators=(",", ":"))), 20_000)
        self.assertTrue(fit["omitted_units"])
        self.assertEqual(list(fit["units"]) + fit["omitted_units"], list(big["units"]))  # display order, nothing lost
        self.assertEqual(fit["name"], big["name"])


if __name__ == "__main__":
    unittest.main()
