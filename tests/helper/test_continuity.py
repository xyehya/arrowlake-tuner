"""G-123: ``tune.last`` (the stored read of this boot, lazily restored after a helper restart) and ``profiles.get`` (what a profile holds).

Through the real helper loop and the simulator; temp directories only. No hardware, no sudo.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from arltuner.helper import ops_tune

from ._measure_support import reference_read
from ._support import Harness, error_code
from .test_tune import ALL_P_53, REPO, TuneCase, row, rows

BOOT_A = "aaaaaaaa-0000-0000-0000-000000000001"
BOOT_B = "bbbbbbbb-0000-0000-0000-000000000002"


class LastReadCase(TuneCase):
    @property
    def state_file(self) -> Path:
        return self.tmp / "state" / "last-read.json"

    def last(self, h: Harness) -> dict[str, Any]:
        r = h.call(f"last{len(h.messages)}", "tune.last", {})
        self.assertTrue(r["ok"], r)
        return r["result"]

    def restart(self, h: Harness, **overrides: Any) -> Harness:
        h.stop()
        return self.start(self.config(**overrides))


class StoreTests(LastReadCase):
    def test_nothing_before_a_read(self) -> None:
        h = self.boot()
        self.assertEqual(self.last(h), {"available": False})

    def test_a_complete_read_is_stored_private_and_served(self) -> None:
        h = self.boot()
        res = self.read(h)
        self.assertTrue(self.state_file.is_file())
        self.assertEqual(stat.S_IMODE(os.stat(self.state_file).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.state_file.parent).st_mode), 0o700)
        rec = json.loads(self.state_file.read_text())
        self.assertEqual((rec["version"], rec["boot_id"], rec["applied_at"]), (1, "11111111-2222-3333-4444-555555555555", None))
        self.assertEqual(sorted(map(int, rec["words"])), sorted(h.helper.last_words))
        got = self.last(h)
        self.assertTrue(got["available"])
        self.assertEqual(got["boot_id"], rec["boot_id"])
        self.assertIsInstance(got["at"], float)
        self.assertEqual(got["result"]["tune"], res["tune"])
        self.assertEqual(got["result"]["units"], res["units"])
        self.assertEqual(got["result"]["tables"], res["tables"])

    def test_a_subset_read_is_not_stored(self) -> None:
        h = self.boot()
        self.read(h, cpus=[0, 8])
        self.assertFalse(self.state_file.exists())
        self.assertEqual(self.last(h), {"available": False})

    def test_another_boot_is_not_served_and_not_restored(self) -> None:
        h = self.boot(boot_id_fn=lambda: BOOT_A)
        self.read(h)
        h2 = self.restart(h, boot_id_fn=lambda: BOOT_B)
        self.assertEqual(self.last(h2), {"available": False})
        r = h2.call("p", "tune.plan", {"intent": rows(row("P0", points={"53": -60}))})
        self.assertEqual(r["error"]["detail"]["check"], "NO_READ")

    def test_an_unknown_boot_id_stores_and_serves_nothing(self) -> None:
        h = self.boot(boot_id_fn=lambda: None)
        self.read(h)
        self.assertFalse(self.state_file.exists())
        self.assertEqual(self.last(h), {"available": False})


class RestartTests(LastReadCase):
    def test_plan_works_after_a_helper_restart_without_a_new_read(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        h2 = self.restart(h)
        self.assertFalse(getattr(h2.helper, "last_words", None))  # nothing in memory yet: the first tune.* call restores
        plan = self.plan(h2, rows(row("P0", points={"53": -60})))  # points by ratio need the words
        self.assertEqual(plan["units"], ["P0"])
        self.assertEqual(sorted(h2.helper.last_words), [*range(8), 8, 12, 16, 20])
        self.assertIsNotNone(h2.helper._tune_state.tables)  # the turbo tables came back too
        self.assertIsNotNone(h2.helper._tune_state.fused)

    def test_a_read_made_in_this_process_wins_over_the_stored_one(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        h2 = self.restart(h)
        h2.helper.last_words = {0: [0x39, 1, 2]}  # a read made in this process (nothing is restored over it)
        self.plan(h2, rows(row("P0", curve=-10)))
        self.assertEqual(h2.helper.last_words, {0: [0x39, 1, 2]})

    def test_the_words_follow_an_apply(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        self.apply_ok(h, self.plan(h, rows(row("P4", curve=-40))))
        h2 = self.restart(h)
        got = self.last(h2)
        self.assertTrue(got["available"])
        self.assertIsNotNone(got["applied_at"])
        oc = got["result"]["tune"]["P4"]["cpus"][0]["points"][0]
        self.assertAlmostEqual(oc["offset_mv"], -40, delta=0.5)  # the whole-curve word reads back as written
        self.assertEqual(got["result"]["differences"], [])
        self.assertTrue(got["result"]["differences_stale"])
        # the restored words are the post-apply ones: a plan back to 0 starts from -40
        plan = self.plan(h2, rows(row("P4", curve=0)))
        self.assertAlmostEqual(plan["before"]["P4"]["0"]["offset_mv"], -40, delta=0.5)

    def test_a_second_read_replaces_the_record(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        first = self.last(h)["at"]
        self.apply_ok(h, self.plan(h, rows(row("P2", curve=-30))))
        self.read(h)
        got = self.last(h)
        self.assertIsNone(got["applied_at"])
        self.assertGreaterEqual(got["at"], first)
        self.assertAlmostEqual(got["result"]["tune"]["P2"]["cpus"][0]["points"][0]["offset_mv"], -30, delta=0.5)


class FileRules(LastReadCase):
    def test_a_planted_symlink_at_the_final_name_is_replaced_not_followed(self) -> None:
        victim = self.tmp / "victim"
        victim.write_text("keep")
        d = self.tmp / "state"
        d.mkdir(mode=0o700)
        os.symlink(victim, d / "last-read.json")
        h = self.boot()
        self.read(h)
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse(self.state_file.is_symlink())
        self.assertTrue(self.last(h)["available"])

    def test_a_symlink_is_never_served(self) -> None:
        h = self.boot()
        self.read(h)
        other = self.tmp / "other.json"
        shutil.copy(self.state_file, other)
        self.state_file.unlink()
        os.symlink(other, self.state_file)
        self.assertEqual(self.last(self.restart(h)), {"available": False})

    def test_a_group_writable_file_or_directory_is_refused(self) -> None:
        h = self.boot()
        self.read(h)
        os.chmod(self.state_file, 0o620)
        h = self.restart(h)
        self.assertEqual(self.last(h), {"available": False})
        os.chmod(self.state_file, 0o600)
        os.chmod(self.state_file.parent, 0o770)
        h = self.restart(h)
        self.assertEqual(self.last(h), {"available": False})
        os.chmod(self.state_file.parent, 0o700)

    def test_a_hard_linked_or_garbage_record_is_refused(self) -> None:
        h = self.boot()
        self.read(h)
        os.link(self.state_file, self.tmp / "alias")
        h = self.restart(h)
        self.assertEqual(self.last(h), {"available": False})
        os.unlink(self.tmp / "alias")
        self.state_file.write_text("{not json")
        h = self.restart(h)
        self.assertEqual(self.last(h), {"available": False})

    def test_an_oversized_record_is_refused(self) -> None:
        h = self.boot()
        self.read(h)
        self.state_file.write_text(" " * (300 * 1024))
        self.assertEqual(self.last(self.restart(h)), {"available": False})

    def test_the_response_fits_the_line(self) -> None:
        h = self.boot()
        self.read(h)
        got = self.last(h)
        self.assertLess(len(json.dumps(got, separators=(",", ":"))), 64 * 1024 - 1000)


class ProfilesGet(TuneCase):
    def accepted(self) -> Path:
        """An accepted state written before G-148: the reference machine's chain of three profiles (CURRENT with three lines)."""
        acc = self.tmp / "accepted"
        (acc / "profiles").mkdir(parents=True)
        names = ("e-p6-split3", "p-boot-ramp", "p-step7")
        (acc / "CURRENT").write_text("".join(f"profile = profiles/{n}.toml\n" for n in names))
        for n in names:
            shutil.copy(REPO / "examples/reference-285k/profiles" / f"{n}.toml", acc / "profiles" / f"{n}.toml")
        return acc

    def get(self, h: Harness, name: str) -> dict[str, Any]:
        r = h.call(f"g{len(h.messages)}", "profiles.get", {"name": name})
        self.assertTrue(r["ok"], r)
        return r["result"]

    def test_a_saved_profile_returns_its_rows_per_unit(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        intent = {"scope": "simple", "rows": [row("P", curve=-20), row("E", points={"46": -35}, v=None, ratio=None)]}
        self.assertTrue(h.call("s", "profiles.save", {"name": "mine", "intent": intent})["ok"])
        got = self.get(h, "mine")
        self.assertEqual((got["name"], got["source"], got["complete"]), ("mine", "saved", True))  # G-148: the read with the edits over it, every unit
        targets = [r["target"] for r in got["contents"]["rows"]]
        self.assertEqual(targets, [f"P{i}" for i in range(8)] + [f"EC{i}" for i in range(4)])
        self.assertEqual(got["contents"]["rows"][0]["offset"], {"kind": "curve", "mv": -20})
        ec0 = got["contents"]["rows"][8]["offset"]
        self.assertEqual((ec0["kind"], ec0["mv"]["46"], ec0["mv"]["40"]), ("points", -35, 0))  # every fused point, the untouched ones at 0
        summary = got["summary"]
        self.assertEqual((summary["units"], summary["points"], summary["deepest_offset_mv"], summary["complete"]), (12, 24, -35, True))
        self.assertEqual(got["contents"]["turbo"], {"P": [57, 57, 55, 55, 55, 55, 55, 55], "E": [48] * 8})  # both tables of the read
        self.assertNotIn("cumulative", got)

    def test_an_older_accepted_chain_lists_its_members_with_the_migrate_hint(self) -> None:
        h = self.boot(accepted_dir=self.accepted())
        listed = h.call("l", "profiles.list", {})["result"]
        self.assertEqual([p["name"] for p in listed["profiles"]], ["e-p6-split3", "p-boot-ramp", "p-step7"])
        self.assertTrue(all(p["chain"] and not p["complete"] for p in listed["profiles"]))
        self.assertIn("accepted-migrate", listed["note"])
        e = self.get(h, "e-p6-split3")
        self.assertEqual((e["source"], e["complete"], e["chain"]), ("accepted", False, True))
        self.assertNotIn("position", e)
        self.assertNotIn("cumulative", e)
        self.assertEqual([r["target"] for r in e["contents"]["rows"]], ["EC0", "EC1", "EC2", "EC3"])  # the member's own file only
        ec0 = e["contents"]["rows"][0]["offset"]
        self.assertEqual((ec0["kind"], ec0["mv"]["46"], ec0["mv"]["40"]), ("points", -35, 0))

    def test_a_curve_offset_with_points_becomes_points_over_every_fused_ratio(self) -> None:
        acc = self.tmp / "accepted"
        (acc / "profiles").mkdir(parents=True)
        (acc / "CURRENT").write_text("policy = policies/x.toml\nprofile = profiles/mix.toml\n")
        (acc / "profiles/mix.toml").write_text(
            '[meta]\nname = "mix"\nplatform = "arl-s-285k"\n[[core]]\nkind = "P"\nindex = 2\noffset_mv = -20\noc_voltage_mv = 1300\nratio = 58\n'
            '[[point]]\nkind = "P"\nindex = 2\npoint = 5\noffset_mv = -50\n[turbo]\np = [57, 57, 55, 55, 55, 55, 55, 55]\n')
        h = self.boot(accepted_dir=acc)
        h.helper.last_words, h.helper.fused_words = reference_read()  # this boot's read: the unit's own fused point ratios
        got = self.get(h, "mix")
        (r,) = got["contents"]["rows"]
        self.assertEqual((r["target"], r["v_target_v"], r["ratio"]), ("P2", 1.3, 58))
        mv = r["offset"]["mv"]
        self.assertEqual(sorted(map(int, mv)), [8, 20, 30, 48, 53, 55, 56, 57])
        self.assertEqual(mv["53"], -50)
        self.assertEqual(mv["48"], -20)
        self.assertEqual(got["contents"]["turbo"], {"P": [57, 57, 55, 55, 55, 55, 55, 55]})
        self.assertTrue(got["summary"]["turbo"])

    def test_errors(self) -> None:
        h = self.boot()
        r = h.call("a", "profiles.get", {"name": "nope"})
        self.assertEqual(error_code(r), "BAD_REQUEST")
        self.assertEqual(r["error"]["detail"]["check"], "PROFILE_NOT_FOUND")
        for bad in ({}, {"name": "Bad Name"}, {"name": "x", "extra": 1}, {"name": 3}):
            with self.subTest(bad=bad):
                self.assertEqual(error_code(h.call(f"b{len(h.messages)}", "profiles.get", bad)), "BAD_REQUEST")

    def test_a_saved_profile_needs_a_read(self) -> None:  # G-148: a saved profile is the complete state of the read, so there is none without one
        h = self.boot()
        r = h.call("s", "profiles.save", {"name": "k", "intent": rows(row("P0", curve=-10))})
        self.assertEqual((error_code(r), r["error"]["detail"]["check"]), ("REFUSED", "NO_READ"))
        self.assertIn("read first", r["error"]["message"])

