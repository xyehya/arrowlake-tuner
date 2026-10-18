"""G-228: save the full state as a profile without accepting it; an accept keeps the state it replaces as ``accepted-before-<stamp>``. Simulator, temp dirs, no hardware."""
from __future__ import annotations

import json
import os
import re
from typing import Any

from arltuner.guard import AuditLog, Request
from arltuner.profiles import load_profile
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
        self.assertEqual(mine[-1]["previous_saved_as"], ["accepted-before-20261018-120000"])

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

    def test_an_older_chain_is_kept_member_by_member(self) -> None:
        h = self.boot_accepting()
        self.accept(h, **FROM_READ)
        first = next((self.acc / "profiles").iterdir())
        (self.acc / "profiles" / "second.toml").write_text(first.read_text())
        current = self.acc / "CURRENT"
        current.write_text(current.read_text() + "profile = profiles/second.toml\n")
        r = self.accept(h, **FROM_READ)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["previous_saved_names"], ["accepted-before-20261018-120000", "accepted-before-20261018-120000-2"])

    def test_store_save_unique(self) -> None:
        store = ProfileStore(self.tmp / "ps", euid=os.geteuid())
        self.assertEqual([store.save_unique("b", profile="x\n")["name"] for _ in range(3)], ["b", "b-2", "b-3"])
        with self.assertRaises(ProfileStoreError) as ctx:
            store.save_unique("B", profile="x\n")
        self.assertEqual(ctx.exception.code, "BAD_NAME")
