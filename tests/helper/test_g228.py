"""G-228: save the full state as a profile without accepting it; an accept keeps the state it replaces as ``accepted-before-<stamp>``. Simulator, temp dirs, no hardware."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any
from unittest import mock

from arltuner.guard import AuditLog, Request
from arltuner.helper.ops_accept import diff_profiles
from arltuner.profiles import RingPoint, RingSetting, TurboTables, load_profile
from arltuner.profiles import store as store_mod
from arltuner.profiles.accepted import AcceptedError, AcceptedStore
from arltuner.profiles.serialize import dump_profile
from arltuner.profiles.store import ProfileStore, ProfileStoreError

from ._support import error_code
from .test_accept import ACCEPT, AcceptCase
from .test_tune import row, rows

FIXED = 1_792_324_800.0  # 2026-10-18T12:00:00Z
FROM_READ: dict[str, Any] = {"from": "read"}


class SaveFromRead(AcceptCase):
    def save(self, h, name: str) -> dict[str, Any]:
        return h.call(f"sv{len(h.messages)}", "profiles.save", {"name": name, **FROM_READ})

    def store(self) -> ProfileStore:
        return ProfileStore(self.audit.parent / "profiles", euid=os.geteuid())

    def test_save_from_read_is_a_complete_profile_and_accepts_nothing(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60}), row("P4", curve=-40)))
        r = self.save(h, "now")
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["result"]["name"], r["result"]["from"], r["result"]["complete"]), ("now", "read", True))
        self.assertFalse(self.acc.exists())  # saved, not accepted
        profile = load_profile(self.store().load("now")["profile"])
        self.assertTrue(profile.complete)
        self.assertEqual(len(profile.entries), 12)
        self.assertIsNotNone(profile.turbo)
        self.assertEqual({(p.index, p.point, p.offset_mv) for p in profile.points if p.kind == "P" and p.index == 3 and p.point == 5}, {(3, 5, -60)})
        got = h.call("g", "profiles.get", {"name": "now"})["result"]
        self.assertEqual((got["source"], got["complete"]), ("saved", True))

    def test_same_words_as_accept_would_record(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60})))
        self.assertTrue(self.save(h, "now")["ok"])
        name = self.accept(h, **FROM_READ)["result"]["name"]
        saved = load_profile(self.store().load("now")["profile"])
        accepted = load_profile(self.accepted_files()[f"profiles/{name}.toml"])
        self.assertEqual((saved.entries, saved.points, saved.turbo, saved.ring), (accepted.entries, accepted.points, accepted.turbo, accepted.ring))

    def test_never_overwrites(self) -> None:
        h = self.boot_accepting()
        self.assertTrue(self.save(h, "now")["ok"])
        r = self.save(h, "now")
        self.assertEqual((error_code(r), r["error"]["detail"]["check"]), ("REFUSED", "EXISTS"))

    def test_needs_a_current_read(self) -> None:
        self.acc = self.tmp / "etc" / "accepted"
        h = self.boot(accepted_dir=self.acc)
        self.assertEqual(self.save(h, "now")["error"]["detail"]["check"], "NO_READ")
        self.read(h)
        AuditLog(self.audit).append("attempt", request=Request(3, 0x11, 0, 5, 0x1234, "another process"), boot_id="x")
        self.assertEqual(self.save(h, "now")["error"]["detail"]["check"], "STALE_READ")

    def test_argument_shapes(self) -> None:
        h = self.boot_accepting()
        bad: list[dict[str, Any]] = [
            {"name": "a", "from": "elsewhere"},
            {"name": "a", **FROM_READ, "intent": {"rows": []}},
            {"name": "A", **FROM_READ},
            dict(FROM_READ),
            {"name": "a", **FROM_READ, "x": 1},
        ]
        for args in bad:
            with self.subTest(args=args):
                self.assertEqual(error_code(h.call("b", "profiles.save", args)), "BAD_REQUEST")
        self.assertTrue(h.call("i", "profiles.save", {"name": "rows", "intent": rows(row("P3", points={"53": -60}))})["ok"])  # the old shape still works


class AcceptKeepsHistory(AcceptCase):
    def boot_accepting(self, *a: Any, **kw: Any):
        return super().boot_accepting(*a, wall_clock=lambda: FIXED, **kw)

    def store(self) -> ProfileStore:
        return ProfileStore(self.audit.parent / "profiles", euid=os.geteuid())

    def saved(self) -> list[str]:
        return [p["name"] for p in self.store().list()]

    def test_the_first_accept_has_nothing_to_keep(self) -> None:
        h = self.boot_accepting()
        r = self.accept(h, **FROM_READ)
        self.assertTrue(r["ok"], r)
        self.assertIsNone(r["result"]["previous_saved_as"])
        self.assertEqual(self.saved(), [])

    def test_a_second_accept_saves_the_replaced_state_complete(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60})))
        first = self.accept(h, **FROM_READ)["result"]["name"]
        before = load_profile(self.accepted_files()[f"profiles/{first}.toml"])
        self.tune(h, rows(row("P3", points={"53": -50}), row("P4", curve=-30)))
        r = self.accept(h, **FROM_READ)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["previous_saved_as"], "accepted-before-20261018-120000")
        self.assertEqual(self.saved(), ["accepted-before-20261018-120000"])
        record = self.store().load("accepted-before-20261018-120000")
        kept = load_profile(record["profile"])
        self.assertTrue(kept.complete)
        self.assertEqual((kept.entries, kept.points, kept.turbo, kept.ring), (before.entries, before.points, before.turbo, before.ring))
        self.assertIn(first, record["profile"].splitlines()[0])  # the comment line says which accepted state this was
        new = load_profile(self.accepted_files()[f"profiles/{r['result']['name']}.toml"])  # and the accepted state is the new one
        self.assertNotEqual(new.points, before.points)
        mine = [json.loads(line) for line in self.audit.read_text().splitlines() if line.strip() and json.loads(line).get("op") == ACCEPT]
        self.assertEqual(mine[-1]["previous_saved_names"], ["accepted-before-20261018-120000"])
        self.assertNotIn("previous_saved_as", mine[-1])

    def test_the_same_second_gets_a_numbered_name(self) -> None:
        h = self.boot_accepting()
        self.accept(h, **FROM_READ)
        names = []
        for _ in range(3):
            r = self.accept(h, **FROM_READ)
            self.assertTrue(r["ok"], r)
            names.append(r["result"]["previous_saved_as"])
        self.assertEqual(names, ["accepted-before-20261018-120000", "accepted-before-20261018-120000-2", "accepted-before-20261018-120000-3"])
        self.assertTrue(all(re.fullmatch(r"[a-z0-9._-]{1,64}", n) for n in names))

    def test_a_saved_profile_accept_keeps_the_previous_state_too(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60})))
        self.accept(h, **FROM_READ)
        intent = rows(row("P3", points={"53": -55}))
        self.tune(h, intent)
        h.call("s", "profiles.save", {"name": "mine", "intent": intent})
        r = self.accept(h, name="mine")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["previous_saved_as"], "accepted-before-20261018-120000")
        self.assertEqual(sorted(self.saved()), ["accepted-before-20261018-120000", "mine"])

    def test_the_previous_state_can_be_re_accepted_only_while_the_cpu_holds_it(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60})))
        self.accept(h, **FROM_READ)
        self.tune(h, rows(row("P3", points={"53": -50})))
        prev = self.accept(h, **FROM_READ)["result"]["previous_saved_as"]
        r = self.accept(h, name=prev)  # the CPU holds -50 now, the kept state is -60: "undo" applies nothing
        self.assertEqual((error_code(r), r["error"]["detail"]["check"]), ("REFUSED", "NOT_APPLIED"))
        self.tune(h, {"profile": prev})  # review and apply it
        self.assertTrue(self.accept(h, name=prev)["ok"])

    def test_when_the_copy_fails_the_accept_is_refused_and_nothing_changes(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60})))
        self.accept(h, **FROM_READ)
        before = self.accepted_files()
        profiles = self.audit.parent / "profiles"
        profiles.mkdir(exist_ok=True)
        profiles.chmod(0o770)  # group-writable: the store refuses it
        r = self.accept(h, **FROM_READ)
        self.assertEqual((error_code(r), r["error"]["detail"]["check"]), ("REFUSED", "PREVIOUS_NOT_SAVED"))
        self.assertEqual(self.accepted_files(), before)

    def test_an_older_chain_is_refused_until_it_is_migrated(self) -> None:
        h = self.boot_accepting()
        self.accept(h, **FROM_READ)
        first = next((self.acc / "profiles").iterdir())
        (self.acc / "profiles" / "second.toml").write_text(first.read_text())
        current = self.acc / "CURRENT"
        current.write_text(current.read_text() + "profile = profiles/second.toml\n")
        before = self.accepted_files()
        r = self.accept(h, **FROM_READ)
        self.assertEqual((error_code(r), r["error"]["detail"]["check"]), ("REFUSED", "ACCEPTED_CHAIN"))
        self.assertIn("accepted-migrate", r["error"]["message"])
        self.assertEqual(self.accepted_files(), before)
        self.assertEqual(self.saved(), [])  # nothing was written anywhere

    def test_store_save_unique(self) -> None:
        store = ProfileStore(self.tmp / "ps", euid=os.geteuid())
        self.assertEqual([store.save_unique("b", profile="x\n")["name"] for _ in range(3)], ["b", "b-2", "b-3"])
        with self.assertRaises(ProfileStoreError) as ctx:
            store.save_unique("B", profile="x\n")
        self.assertEqual(ctx.exception.code, "BAD_NAME")

    def test_store_save_unique_runs_out_of_names(self) -> None:
        store = ProfileStore(self.tmp / "ps", euid=os.geteuid())
        for name in ["b", *(f"b-{n}" for n in range(2, 100))]:
            store.save(name, profile="x\n")
        with self.assertRaises(ProfileStoreError) as ctx:
            store.save_unique("b", profile="x\n")
        self.assertEqual(ctx.exception.code, "EXISTS")
        self.assertIn("-99", ctx.exception.message)
        self.assertEqual(len(store.list()), 99)

    def test_previous_states_do_not_count_toward_the_profile_limit(self) -> None:
        h = self.boot_accepting()
        self.accept(h, **FROM_READ)
        with mock.patch.object(store_mod, "MAX_PROFILES", 1):
            self.assertTrue(h.call("s1", "profiles.save", {"name": "mine", **FROM_READ})["ok"])
            full = h.call("s2", "profiles.save", {"name": "other", **FROM_READ})
            self.assertEqual((error_code(full), full["error"]["detail"]["check"]), ("BAD_REQUEST", "TOO_BIG"))
            self.assertIn(str(self.audit.parent / "profiles"), full["error"]["message"])
            for _ in range(3):  # a full library never blocks an accept: the replaced state is still kept
                r = self.accept(h, **FROM_READ)
                self.assertTrue(r["ok"], r)
        self.assertEqual(len(self.saved()), 4)

    def test_previous_states_have_a_cap_of_their_own_and_the_refusal_names_the_directory(self) -> None:
        h = self.boot_accepting()
        self.accept(h, **FROM_READ)
        self.assertTrue(self.accept(h, **FROM_READ)["ok"])
        before = self.accepted_files()
        with mock.patch.object(store_mod, "MAX_PREVIOUS", 1):
            r = self.accept(h, **FROM_READ)
        self.assertEqual((error_code(r), r["error"]["detail"]["check"]), ("REFUSED", "PREVIOUS_NOT_SAVED"))
        self.assertIn(str(self.audit.parent / "profiles"), r["error"]["message"])
        self.assertEqual(self.accepted_files(), before)

    def test_the_list_shows_the_newest_previous_states_after_the_saved_profiles(self) -> None:
        store = ProfileStore(self.tmp / "ps", euid=os.geteuid())
        store.save("zeta", profile="x\n")
        extra = store_mod._MAX_LISTED_PREVIOUS + 5
        for n in range(extra):
            store.save(f"accepted-before-20261018-{n:06d}", profile="x\n")
        names = [p["name"] for p in store.list()]
        self.assertEqual(names[0], "zeta")
        self.assertEqual(len(names), 1 + store_mod._MAX_LISTED_PREVIOUS)
        self.assertEqual(names[-1], f"accepted-before-20261018-{extra - 1:06d}")  # the newest are listed, the oldest are not

    def test_a_failure_after_the_copy_says_the_state_was_saved_and_the_accepted_state_is_unchanged(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60})))
        self.accept(h, **FROM_READ)
        before = self.accepted_files()
        self.tune(h, rows(row("P3", points={"53": -50})))
        with mock.patch.object(AcceptedStore, "replace", side_effect=AcceptedError("IO", "cannot write /etc/arltuner/accepted: No space left on device")):
            r = self.accept(h, **FROM_READ)
        self.assertEqual(error_code(r), "REFUSED")
        message = r["error"]["message"]
        self.assertIn("No space left on device", message)
        self.assertIn("The previous accepted state was saved as accepted-before-20261018-120000; the accepted state is unchanged.", message)
        self.assertNotIn("Nothing was changed", message)
        self.assertEqual(self.accepted_files(), before)
        self.assertEqual(self.saved(), ["accepted-before-20261018-120000"])  # the stray copy is harmless and named

    def test_an_audit_failure_after_the_copy_says_so_too(self) -> None:
        h = self.boot_accepting()
        self.accept(h, **FROM_READ)
        before = self.accepted_files()

        class Broken:
            def __init__(self, *_a: Any, **_k: Any) -> None:
                pass

            def append(self, *_a: Any, **_k: Any) -> None:
                raise OSError("disk full")

        with mock.patch("arltuner.helper.ops_accept.AuditLog", Broken):
            r = self.accept(h, **FROM_READ)
        self.assertEqual((error_code(r), r["error"]["detail"]["check"]), ("REFUSED", "AUDIT_UNAVAILABLE"))
        self.assertIn("saved as accepted-before-20261018-120000; the accepted state is unchanged", r["error"]["message"])
        self.assertEqual(self.accepted_files(), before)

    def test_the_header_of_a_state_saved_from_the_read_does_not_talk_about_the_editor(self) -> None:
        h = self.boot_accepting()
        self.assertTrue(h.call("a", "profiles.save", {"name": "now", **FROM_READ})["ok"])
        self.assertTrue(h.call("b", "profiles.save", {"name": "rows", "intent": rows(row("P3", points={"53": -60}))})["ok"])
        self.assertNotIn("Tune editor", self.store().load("now")["profile"])
        self.assertIn("as it is", self.store().load("now")["profile"])
        self.assertIn("Tune editor", self.store().load("rows")["profile"])


class AcceptOverAnUnreadableState(AcceptCase):
    def boot_accepting(self, *a: Any, **kw: Any):
        return super().boot_accepting(*a, wall_clock=lambda: FIXED, **kw)

    def reset(self) -> None:
        shutil.rmtree(self.acc, ignore_errors=True)
        for extra in self.acc.parent.glob("accepted.unreadable-*"):
            shutil.rmtree(extra)
        shutil.rmtree(self.audit.parent / "profiles", ignore_errors=True)

    def test_an_empty_or_garbled_current_is_refused_with_the_way_out_and_nothing_is_changed(self) -> None:
        h = self.boot_accepting()
        for label, current in (("empty", ""), ("garbage", "this is not an accepted state\n\x01"), ("a path outside", "profile = ../../etc/passwd\n")):
            with self.subTest(current=label):
                self.assertTrue(self.accept(h, **FROM_READ)["ok"])
                (self.acc / "CURRENT").write_text(current)
                before = self.accepted_files()
                r = self.accept(h, **FROM_READ)
                self.assertEqual((error_code(r), r["error"]["detail"]["check"]), ("REFUSED", "ACCEPTED_UNREADABLE"))
                message = r["error"]["message"]
                self.assertNotIn(".)", message)
                self.assertNotIn("(.", message)
                self.assertIn("moved aside", message)
                self.assertIn(f"{self.acc}.unreadable-20261018-120000", message)
                self.assertNotIn("by hand", message)
                self.assertEqual(self.accepted_files(), before)
                self.reset()

    def test_with_the_flag_the_whole_directory_is_moved_aside_then_the_accept_goes_ahead(self) -> None:
        h = self.boot_accepting()
        for label, current in (("empty", ""), ("garbage", "xx\n")):
            with self.subTest(current=label):
                self.assertTrue(self.accept(h, **FROM_READ)["ok"])
                (self.acc / "CURRENT").write_text(current)
                old = self.accepted_files()
                r = self.accept(h, set_aside_unreadable=True, **FROM_READ)
                self.assertTrue(r["ok"], r)
                aside = Path(r["result"]["set_aside_as"])
                self.assertEqual(aside, self.acc.parent / "accepted.unreadable-20261018-120000")
                self.assertEqual({p.relative_to(aside).as_posix(): p.read_text() for p in sorted(aside.rglob("*")) if p.is_file()}, old)  # everything is kept, as it was
                self.assertIsNone(r["result"]["previous_saved_as"])  # an unreadable state cannot be a profile
                self.assertIn("profile = profiles/", self.accepted_files()["CURRENT"])
                record = [json.loads(line) for line in self.audit.read_text().splitlines() if line.strip() and json.loads(line).get("op") == ACCEPT][-1]
                self.assertEqual(record["set_aside_as"], str(aside))
                self.reset()

    def test_a_directory_without_current_but_with_files_is_not_silently_deleted(self) -> None:
        h = self.boot_accepting()
        self.acc.mkdir()
        (self.acc / "EXPECTED-words.txt").write_text("0  1 2 3\n")
        r = self.accept(h, **FROM_READ)
        self.assertEqual((error_code(r), r["error"]["detail"]["check"]), ("REFUSED", "ACCEPTED_UNREADABLE"))
        self.assertTrue((self.acc / "EXPECTED-words.txt").exists())
        r = self.accept(h, set_aside_unreadable=True, **FROM_READ)
        self.assertTrue(r["ok"], r)
        self.assertEqual((Path(r["result"]["set_aside_as"]) / "EXPECTED-words.txt").read_text(), "0  1 2 3\n")

    def test_an_empty_directory_is_a_first_accept(self) -> None:
        h = self.boot_accepting()
        self.acc.mkdir()
        r = self.accept(h, **FROM_READ)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["result"]["set_aside_as"], r["result"]["previous_saved_as"]), (None, None))

    def test_the_flag_changes_nothing_when_the_state_is_readable(self) -> None:
        h = self.boot_accepting()
        self.assertTrue(self.accept(h, **FROM_READ)["ok"])
        r = self.accept(h, set_aside_unreadable=True, **FROM_READ)
        self.assertTrue(r["ok"], r)
        self.assertIsNone(r["result"]["set_aside_as"])
        self.assertEqual(r["result"]["previous_saved_as"], "accepted-before-20261018-120000")
        self.assertEqual(sorted(p.name for p in self.acc.parent.iterdir()), ["accepted"])

    def test_if_the_new_state_cannot_be_written_the_unreadable_one_goes_back(self) -> None:
        h = self.boot_accepting()
        self.assertTrue(self.accept(h, **FROM_READ)["ok"])
        (self.acc / "CURRENT").write_text("")
        old = self.accepted_files()
        with mock.patch.object(AcceptedStore, "replace", side_effect=AcceptedError("IO", "cannot write")):
            r = self.accept(h, set_aside_unreadable=True, **FROM_READ)
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(self.accepted_files(), old)
        self.assertEqual(sorted(p.name for p in self.acc.parent.iterdir()), ["accepted"])

    def test_the_flag_must_be_true_when_given(self) -> None:
        h = self.boot_accepting()
        for bad in (False, "yes", 1, None):
            with self.subTest(bad=bad):
                self.assertEqual(error_code(self.accept(h, set_aside_unreadable=bad, **FROM_READ)), "BAD_REQUEST")
        self.assertEqual(error_code(self.accept(h, set_aside_unreadable=True)), "BAD_REQUEST")  # still exactly one of name / from


class ProfilesDiff(AcceptCase):
    def diff(self, h, a: str, b: str) -> dict[str, Any]:
        return h.call(f"df{len(h.messages)}", "profiles.diff", {"a": a, "b": b})

    def store(self) -> ProfileStore:
        return ProfileStore(self.audit.parent / "profiles", euid=os.geteuid())

    def labels(self, r: dict[str, Any]) -> list[tuple[str, str]]:
        return [(row["unit"], row["label"]) for row in r["result"]["rows"]]

    def test_the_accepted_state_equals_the_read_it_was_made_from(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60}), row("P4", curve=-40)))
        self.assertTrue(self.accept(h, **FROM_READ)["ok"])
        r = self.diff(h, "accepted", "read")
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["result"]["rows"], r["result"]["units"]), ([], []))
        self.assertIn("the turbo tables", r["result"]["compared"])
        self.assertEqual((r["result"]["a"]["source"], r["result"]["b"]["source"]), ("accepted", "read"))

    def test_a_point_change_and_an_oc_word_offset_only_change_are_both_listed(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60}), row("P4", curve=-40)))
        self.assertTrue(self.accept(h, **FROM_READ)["ok"])
        self.tune(h, rows(row("P3", points={"53": -50}), row("P4", curve=-30), row("P5", curve=-20)))  # P3 point, P4 / P5 whole-curve (OC word) only
        r = self.diff(h, "accepted", "read")
        got = {(row["unit"], row["label"]): (row["kind"], row["a"], row["b"]) for row in r["result"]["rows"]}
        self.assertEqual(got[("P3", "Offset at 53x")], ("mv", -60, -50))
        self.assertEqual(got[("P4", "OC word: whole-curve offset")], ("mv", -40, -30))
        self.assertEqual(got[("P5", "OC word: whole-curve offset")], ("mv", 0, -20))
        self.assertEqual({u["unit"]: u["n"] for u in r["result"]["units"]}, {"P3": 1, "P4": 1, "P5": 1})
        self.assertEqual([u["unit"] for u in r["result"]["units"]], ["P3", "P4", "P5"])  # unit order

    def test_an_e_core_oc_ratio_and_target_voltage_are_listed(self) -> None:
        h = self.boot_accepting()
        self.assertTrue(self.accept(h, **FROM_READ)["ok"])
        self.tune(h, rows(row("EC0", ratio=49, v=1.1)))
        got = {(row["unit"], row["label"]): (row["kind"], row["a"], row["b"]) for row in self.diff(h, "accepted", "read")["result"]["rows"]}
        self.assertEqual(got[("EC0", "OC ratio")], ("x", None, 49))
        self.assertEqual(got[("EC0", "OC target voltage")], ("v", None, 1.1))

    def test_saved_profiles_compare_and_the_read_needs_a_current_read(self) -> None:
        h = self.boot_accepting()
        self.assertTrue(h.call("s1", "profiles.save", {"name": "one", **FROM_READ})["ok"])
        self.tune(h, rows(row("P3", points={"53": -60})))
        self.assertTrue(h.call("s2", "profiles.save", {"name": "two", **FROM_READ})["ok"])
        r = self.diff(h, "one", "two")
        self.assertEqual(self.labels(r), [("P3", "Offset at 53x")])
        self.assertEqual((r["result"]["a"]["name"], r["result"]["b"]["source"]), ("one", "saved"))
        self.assertEqual(error_code(self.diff(h, "one", "nope")), "BAD_REQUEST")
        no_accept = self.diff(h, "accepted", "one")
        self.assertEqual((error_code(no_accept), no_accept["error"]["detail"]["check"]), ("REFUSED", "NO_ACCEPTED_STATE"))
        AuditLog(self.audit).append("attempt", request=Request(3, 0x11, 0, 5, 0x1234, "another process"), boot_id="x")
        stale = self.diff(h, "one", "read")
        self.assertEqual(stale["error"]["detail"]["check"], "STALE_READ")

    def test_no_read_is_named(self) -> None:
        self.acc = self.tmp / "etc" / "accepted"
        h = self.boot(accepted_dir=self.acc)
        self.assertTrue(self.store().directory.parent.exists())
        r = self.diff(h, "read", "read")
        self.assertEqual(r["error"]["detail"]["check"], "NO_READ")

    def test_argument_shapes(self) -> None:
        h = self.boot_accepting()
        for args in ({}, {"a": "accepted"}, {"b": "read"}, {"a": "Bad Name", "b": "read"}, {"a": 1, "b": "read"}, {"a": "x", "b": "read", "c": 1}):
            with self.subTest(args=args):
                self.assertEqual(error_code(h.call("b", "profiles.diff", args)), "BAD_REQUEST")

    def test_ring_only_and_turbo_only_differences_are_found_in_the_profiles_themselves(self) -> None:
        h = self.boot_accepting()
        self.assertTrue(h.call("s1", "profiles.save", {"name": "base", **FROM_READ})["ok"])
        base = load_profile(self.store().load("base")["profile"])
        self.assertIsNotNone(base.ring)  # the simulator's read has the ring
        ring = RingSetting(0x28, (RingPoint(1, 0, 8), RingPoint(2, -25, 14)))
        bare = dataclasses.replace(base, name="bare", ring=None)
        with_ring = dataclasses.replace(base, name="ringed", ring=ring)
        other_ring = dataclasses.replace(base, name="ringed2", ring=RingSetting(0x27, (RingPoint(1, 0, 8), RingPoint(2, -20, 14))))
        for p in (bare, with_ring, other_ring):
            self.store().save(p.name, profile=dump_profile(p, "test"))
        r = self.diff(h, "bare", "ringed")
        self.assertEqual(self.labels(r), [("ring", "OC word (BIOS-owned)"), ("ring", "Offset at 8x"), ("ring", "Offset at 14x")])
        self.assertEqual([row["a"] for row in r["result"]["rows"]], [None, None, None])
        self.assertIn("the ring", r["result"]["compared"])
        r = self.diff(h, "ringed", "ringed2")
        self.assertEqual([(row["label"], row["a"], row["b"]) for row in r["result"]["rows"]], [("OC word (BIOS-owned)", "0x00000028", "0x00000027"), ("Offset at 14x", -25, -20)])
        # the turbo tables
        t = base.turbo
        self.assertIsNotNone(t)
        bumped = dataclasses.replace(base, turbo=TurboTables(e=t.e, p=tuple(x + 1 if i == 3 else x for i, x in enumerate(t.p))))
        self.assertEqual([(row["unit"], row["label"]) for row in diff_profiles(base, bumped, self.topology(h))], [("P turbo", "Bucket 3")])

    def topology(self, h):
        from arltuner.helper import ops_tune as T

        return T._topology(h.helper)

    def test_what_is_not_compared_is_named(self) -> None:
        h = self.boot_accepting()
        self.assertTrue(h.call("s1", "profiles.save", {"name": "one", **FROM_READ})["ok"])
        base = load_profile(self.store().load("one")["profile"])
        self.store().save("bare", profile=dump_profile(dataclasses.replace(base, name="bare", turbo=None, ring=None), "test"))
        self.store().save("bare2", profile=dump_profile(dataclasses.replace(base, name="bare2", turbo=None, ring=None), "test"))
        r = self.diff(h, "bare", "bare2")["result"]
        self.assertEqual(r["rows"], [])
        self.assertEqual(sorted(x["what"] for x in r["not_compared"]), ["the ring", "the turbo tables"])
