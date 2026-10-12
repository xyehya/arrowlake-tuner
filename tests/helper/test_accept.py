"""G-129: ``profiles.accept`` (D-036) through the real helper loop and the simulator. The accepted state is the user's: written here, never shipped.

No hardware, no sudo. The accepted directory is a temp directory owned by the test user (the helper's euid), like the root-owned ``/etc/arltuner/accepted``.
"""
from __future__ import annotations

import json
from typing import Any

from arltuner.helper.ops import _parse_expected
from arltuner.mailbox import VfWord
from arltuner.profiles import load_profile
from arltuner.profiles.fingerprint import parse_fingerprint
from arltuner.topology import arl_s_285k

from ._support import error_code
from .test_tune import ALL_P_53, TuneCase, row, rows

ACCEPT = "profiles.accept"


class AcceptCase(TuneCase):
    def boot_accepting(self, config: Any = ALL_P_53, **overrides: Any):
        self.acc = self.tmp / "etc" / "accepted"
        self.acc.parent.mkdir(exist_ok=True)
        self.acc.parent.chmod(0o755)
        h = self.boot(config, accepted_dir=self.acc, **overrides)
        self.read(h)
        return h

    def tune(self, h, intent: dict[str, Any]) -> None:
        self.apply_ok(h, self.plan(h, intent))
        self.read(h)  # the page re-reads after an apply; the stored words are the verified read-backs either way

    def accept(self, h, **args: Any) -> dict[str, Any]:
        return h.call(f"acc{len(h.messages)}", ACCEPT, args)

    def accepted_files(self) -> dict[str, str]:
        return {p.relative_to(self.acc).as_posix(): p.read_text() for p in sorted(self.acc.rglob("*")) if p.is_file()}


class AcceptSaved(AcceptCase):
    def test_a_saved_profile_that_is_applied_becomes_the_accepted_state(self) -> None:
        h = self.boot_accepting()
        intent = rows(row("P3", points={"53": -60}), row("P4", curve=-40))
        self.tune(h, intent)
        self.assertTrue(h.call("s", "profiles.save", {"name": "mine", "intent": intent})["ok"])
        r = self.accept(h, name="mine")
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["result"]["name"], r["result"]["from"], r["result"]["replaced"]), ("mine", "saved", []))
        files = self.accepted_files()
        self.assertEqual(sorted(files), ["CURRENT", "EXPECTED-words.txt", "fingerprint.json", "profiles/mine.toml"])
        self.assertIn("profile = profiles/mine.toml\n", files["CURRENT"])
        self.assertFalse([line for line in files["CURRENT"].splitlines() if line.startswith("policy")])  # no policy line: the restore uses the installed policy
        profile = load_profile(files["profiles/mine.toml"])
        self.assertEqual(profile.name, "mine")
        self.assertEqual({(p.kind, p.index, p.point, p.offset_mv, p.expect_ratio) for p in profile.points if p.index == 3 and p.point == 5}, {("P", 3, 5, -60, 53)})
        self.assertEqual({(c.kind, c.index, c.offset_mv) for c in profile.entries}, {("P", 4, -40)})
        # the words file is what the chip holds, in the format accepted_match reads
        table = _parse_expected(files["EXPECTED-words.txt"])
        self.assertEqual(sorted(table), [*range(8), 8, 12, 16, 20])
        self.assertEqual(VfWord.from_u32(table[3][5]).offset_raw, VfWord.mv_to_offset_raw(-60))
        self.assertEqual(VfWord.from_u32(table[4][0]).offset_raw, VfWord.mv_to_offset_raw(-40))

    def test_the_dashboard_compare_matches_right_after_accepting(self) -> None:
        h = self.boot_accepting()
        before = self.read(h)
        self.assertIsNone(before["accepted_match"])
        self.assertTrue(before["accepted_note"].startswith("No accepted state yet"), before["accepted_note"])  # D-036: a fresh system has none
        intent = rows(row("P3", points={"53": -60}))
        self.tune(h, intent)
        h.call("s", "profiles.save", {"name": "mine", "intent": intent})
        self.assertTrue(self.accept(h, name="mine")["ok"])
        after = self.read(h)
        self.assertIsNone(after["accepted_note"])
        self.assertTrue(after["accepted_match"] and all(c["match"] for c in after["accepted_match"]), after["accepted_match"])
        listed = h.call("l", "profiles.list", {})["result"]
        self.assertTrue(listed["accepted"])
        self.assertIn({"name": "mine", "source": "accepted", "created": None}, listed["profiles"])

    def test_tune_last_compares_with_the_state_accepted_after_the_read(self) -> None:
        """No apply this boot (applied_at is null): the stored read must still be compared with an accepted state that appeared after it."""
        h = self.boot_accepting()
        stored = h.call("l0", "tune.last", {})["result"]["result"]
        self.assertIsNone(stored["accepted_match"])
        self.assertTrue(stored["accepted_note"].startswith("No accepted state yet"))
        self.assertTrue(self.accept(h, **{"from": "read"})["ok"])
        last = h.call("l1", "tune.last", {})["result"]
        self.assertIsNone(last["applied_at"])
        self.assertIsNone(last["result"]["accepted_note"])
        self.assertTrue(last["result"]["accepted_match"] and all(c["match"] for c in last["result"]["accepted_match"]), last["result"]["accepted_match"])

    def test_restore_accepted_puts_the_accepted_words_back(self) -> None:
        h = self.boot_accepting()
        intent = rows(row("P3", points={"53": -60}, ), row("P2", points={"53": -30}))
        self.tune(h, intent)
        h.call("s", "profiles.save", {"name": "mine", "intent": intent})
        self.assertTrue(self.accept(h, name="mine")["ok"])
        self.tune(h, {"reset": "stock"})
        self.assertEqual(self.word(3, 5).offset_raw, 0)
        self.tune(h, {"restore": "accepted"})
        self.assertEqual(self.word(3, 5).offset_raw, VfWord.mv_to_offset_raw(-60))
        self.assertEqual(self.word(2, 5).offset_raw, VfWord.mv_to_offset_raw(-30))
        res = self.read(h)
        self.assertTrue(all(c["match"] for c in res["accepted_match"]), res["accepted_match"])

    def test_a_saved_profile_that_was_never_applied_is_refused(self) -> None:
        h = self.boot_accepting()
        h.call("s", "profiles.save", {"name": "later", "intent": rows(row("P3", points={"53": -60}))})
        r = self.accept(h, name="later")
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(r["error"]["detail"]["check"], "NOT_APPLIED")
        self.assertIn("Apply it first", r["error"]["message"])
        self.assertFalse(self.acc.exists())

    def test_an_unknown_saved_profile_and_bad_arguments(self) -> None:
        h = self.boot_accepting()
        self.assertEqual(error_code(self.accept(h, name="nope")), "BAD_REQUEST")
        for args in ({}, {"name": "a", "from": "read"}, {"from": "elsewhere"}, {"name": "../x"}, {"name": "..x"}, {"name": ".x"}, {"name": "A"}, {"name": 3}, {"name": "a", "extra": 1}):
            with self.subTest(args=args):
                self.assertEqual(error_code(self.accept(h, **args)), "BAD_REQUEST")
        self.assertFalse(self.acc.exists())


class AcceptFromRead(AcceptCase):
    def test_the_state_of_the_last_read_can_be_accepted_and_restored(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60}), row("P4", curve=-40), row("P5", points={"55": -20})))
        r = self.accept(h, **{"from": "read"})
        self.assertTrue(r["ok"], r)
        name = r["result"]["name"]
        self.assertRegex(name, r"^read-\d{8}-\d{6}$")
        files = self.accepted_files()
        got = h.call("g", "profiles.get", {"name": name})  # the page asks for what every listed profile holds: the name must be one profiles.get takes
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["result"]["source"], "accepted")
        profile = load_profile(files[f"profiles/{name}.toml"])
        have = {(p.index, p.point): (p.offset_mv, p.expect_ratio) for p in profile.points if p.kind == "P"}
        self.assertEqual(have[(3, 5)], (-60, 53))
        self.assertEqual(have[(5, 6)], (-20, 55))
        self.assertEqual(have[(0, 5)], (0, 53))  # every fused point is recorded, 0 mV included: a restore also clears a stray offset
        self.assertEqual({(c.kind, c.index, c.offset_mv) for c in profile.entries if c.offset_mv}, {("P", 4, -40)})
        self.tune(h, {"reset": "stock"})
        self.tune(h, {"restore": "accepted"})
        res = self.read(h)
        self.assertTrue(res["accepted_match"] and all(c["match"] for c in res["accepted_match"]), res["accepted_match"])

    def test_accepting_replaces_the_whole_previous_state(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60})))
        first = self.accept(h, **{"from": "read"})["result"]["name"]
        self.tune(h, rows(row("P3", points={"53": -61})))
        second = self.accept(h, **{"from": "read"})
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["result"]["replaced"], [first])
        files = self.accepted_files()
        self.assertEqual(sorted(files), ["CURRENT", "EXPECTED-words.txt", "fingerprint.json", f"profiles/{second['result']['name']}.toml"])
        self.assertEqual(sorted(p.name for p in self.acc.parent.iterdir()), ["accepted"], "no staging leftovers")
        table = _parse_expected(files["EXPECTED-words.txt"])
        self.assertEqual(VfWord.from_u32(table[3][5]).offset_raw, VfWord.mv_to_offset_raw(-61))

    def test_a_word_that_millivolts_cannot_record_is_refused_not_rounded(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60})))
        bad = next(raw for raw in range(-300, -1) if VfWord.mv_to_offset_raw(round(raw * 1000 / 1024)) != raw)  # a raw offset no whole-mV profile entry reproduces
        self.sim._vf[self.sim._vf_key(0, 3, 5)] = self.sim.vf(3, 0, 5).with_(offset_raw=bad)
        self.read(h)
        r = self.accept(h, **{"from": "read"})
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(r["error"]["detail"]["check"], "NOT_REPRESENTABLE")
        self.assertFalse(self.acc.exists())

    def test_the_audit_log_gets_one_record(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60})))
        name = self.accept(h, **{"from": "read"})["result"]["name"]
        records = [json.loads(line) for line in self.audit.read_text().splitlines() if line.strip()]
        mine = [r for r in records if r.get("op") == ACCEPT]
        self.assertEqual(len(mine), 1)
        self.assertEqual((mine[0]["event"], mine[0]["accepted"], mine[0]["replaced"]), ("result", name, []))
        self.assertNotIn("cmd", mine[0])  # not a mailbox record: neither the per-boot count nor the tuned-this-boot rule sees it


class AcceptRefusals(AcceptCase):
    def test_nothing_without_a_read_this_boot(self) -> None:
        self.acc = self.tmp / "etc" / "accepted"
        self.acc.parent.mkdir()
        self.acc.parent.chmod(0o755)
        h = self.boot(accepted_dir=self.acc)
        r = self.accept(h, **{"from": "read"})
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(r["error"]["detail"]["check"], "NO_READ")
        self.assertFalse(self.acc.exists())

    def test_a_helper_without_an_accepted_directory_refuses(self) -> None:
        h = self.boot(ALL_P_53)
        self.read(h)
        r = self.accept(h, **{"from": "read"})
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(r["error"]["detail"]["check"], "NO_ACCEPTED_DIR")

    def test_a_group_writable_parent_is_not_trusted(self) -> None:
        h = self.boot_accepting()
        self.acc.parent.chmod(0o775)
        r = self.accept(h, **{"from": "read"})
        self.assertEqual(error_code(r), "REFUSED")
        self.assertEqual(r["error"]["detail"]["check"], "NO_ACCEPTED_STATE")
        self.assertFalse(self.acc.exists())

    def test_a_symlink_at_the_accepted_path_is_moved_aside_never_followed(self) -> None:
        h = self.boot_accepting()
        victim = self.tmp / "victim"
        victim.mkdir()
        (victim / "precious").write_text("keep")
        self.acc.symlink_to(victim)
        r = self.accept(h, **{"from": "read"})
        self.assertTrue(r["ok"], r)
        self.assertTrue(self.acc.is_dir() and not self.acc.is_symlink())
        self.assertEqual(sorted(p.name for p in victim.iterdir()), ["precious"])

    def test_the_policy_still_judges_a_saved_profile(self) -> None:
        h = self.boot_accepting()
        self.assertTrue(h.call("s", "profiles.save", {"name": "wild", "intent": rows(row("P3", curve=-150))})["ok"])
        self.policy.write_text(self.policy.read_text().replace("effective_offset_floor_mv = -200", "effective_offset_floor_mv = -50"))
        r = self.accept(h, name="wild")
        self.assertEqual(error_code(r), "REFUSED")
        self.assertFalse(self.acc.exists())

    def test_accept_is_not_a_hardware_write(self) -> None:
        h = self.boot_accepting()
        self.tune(h, rows(row("P3", points={"53": -60})))
        sent = len(self.sim.requests)
        self.assertTrue(self.accept(h, **{"from": "read"})["ok"])
        self.assertEqual(len(self.sim.requests), sent)
        self.assertEqual(sorted(p.name for p in self.acc.parent.iterdir()), ["accepted"])


CPUINFO = """processor\t: 0
vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 198
model name\t: Intel(R) Core(TM) Ultra 9 285K
stepping\t: 2

processor\t: 1
vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 198
model name\t: Intel(R) Core(TM) Ultra 9 285K
"""


class AcceptFingerprint(AcceptCase):
    """G-138: ``fingerprint.json`` next to the accepted words: per Linux CPU its kind / cluster / fused 0x09 word, plus the CPU's identity."""

    def test_the_fingerprint_records_every_cpu_with_its_fused_word_and_the_cpu_identity(self) -> None:
        cpuinfo = self.tmp / "cpuinfo"
        cpuinfo.write_text(CPUINFO)
        h = self.boot_accepting(cpuinfo_path=cpuinfo)
        self.tune(h, rows(row("P3", points={"53": -60})))
        r = self.accept(h, **{"from": "read"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["fingerprint"], "complete")
        fp = parse_fingerprint(self.accepted_files()["fingerprint.json"])
        self.assertEqual((fp.cpu_id.vendor, fp.cpu_id.family, fp.cpu_id.model, fp.cpu_id.brand), ("GenuineIntel", 6, 198, "Intel(R) Core(TM) Ultra 9 285K"))
        self.assertEqual(fp.topology(), arl_s_285k())
        topo = fp.require_restorable()
        self.assertEqual([c.cpu for c in fp.cpus], list(range(24)))
        self.assertEqual(len({fp.word_of(c) for c in topo.cpus_of_kind("P")}), 8)  # distinct per P-core
        self.assertEqual({fp.word_of(c) for c in range(12, 16)}, {fp.word_of(12)})  # shared inside an E cluster
        self.assertEqual([c.cpu for c in fp.cpus if c.favored], [0, 3])

    def test_under_the_simulator_no_cpu_identity_is_recorded_and_the_restore_would_refuse(self) -> None:
        h = self.boot_accepting()  # cpuinfo_path unset: the host's /proc/cpuinfo is never read
        self.tune(h, rows(row("P3", points={"53": -60})))
        r = self.accept(h, **{"from": "read"})
        self.assertTrue(r["ok"], r)
        self.assertIn("no CPU identity", r["result"]["fingerprint"])
        self.assertIsNone(parse_fingerprint(self.accepted_files()["fingerprint.json"]).cpu_id)

    def test_an_unreadable_cpuinfo_records_no_identity(self) -> None:
        h = self.boot_accepting(cpuinfo_path=self.tmp / "missing")
        self.tune(h, rows(row("P3", points={"53": -60})))
        r = self.accept(h, **{"from": "read"})
        self.assertTrue(r["ok"], r)
        self.assertIn("incomplete", r["result"]["fingerprint"])
