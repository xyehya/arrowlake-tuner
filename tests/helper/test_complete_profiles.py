"""G-148 (D-044): complete, independent profiles; the accepted state is one profile, restored in one shot.

Simulator flows through the real helper process loop and the REAL guard (no hardware): a cold boot restored in one apply (also with a small step limit, which
the apply splits into rounds), a warm boot with a residual OC point (EQUIVALENT), saving a complete profile, accepting a partial (legacy) profile that was applied
(the complete state is recorded), and the refusal of a complete profile made on another CPU. The accepted state is the reference machine's, converted to one
complete profile (``examples/reference-285k/profiles``: ``CURRENT`` + ``accepted-2026-10-04.toml`` + ``EXPECTED-words.txt``).
"""
from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path
from typing import Any

from arltuner.guard.policy import fused_top_ratio
from arltuner.helper.ops import _parse_expected
from arltuner.mailbox import VfWord
from arltuner.msr import SimConfig, SimMsrPort
from arltuner.profiles import load_profile
from arltuner.profiles.store import ProfileStore
from arltuner.topology import arl_s_285k

from ._support import error_code
from .test_tune import REPO, TuneCase, row, rows, texts

EXAMPLE = REPO / "examples/reference-285k/profiles"
EXPECTED = _parse_expected((EXAMPLE / "EXPECTED-words.txt").read_text())
TOPO = arl_s_285k()
COLD = SimConfig.measured(per_point=True, reject_zero_fields=True, sticky_oc_ratio=True)
EC1, EC1_WORD = (12, 13, 14, 15), VfWord(48, 0x48C, False, 0).to_u32()  # [MEASURED lnx 2026-10-13] a residual OC point after a warm reboot (G-147)


def unit_words(sim: SimMsrPort, cpu: int, n: int) -> list[int]:
    return [sim.vf(cpu, 0, p).to_u32() for p in range(n)]


class CompleteCase(TuneCase):
    def accepted(self) -> Path:
        """The reference machine's accepted state as the app now writes it: CURRENT with one line, the complete profile, EXPECTED-words.txt."""
        acc = self.tmp / "accepted"
        (acc / "profiles").mkdir(parents=True)
        shutil.copy(EXAMPLE / "CURRENT", acc / "CURRENT")
        shutil.copy(EXAMPLE / "EXPECTED-words.txt", acc / "EXPECTED-words.txt")
        shutil.copy(EXAMPLE / "accepted-2026-10-04.toml", acc / "profiles" / "accepted-2026-10-04.toml")
        return acc

    def policy_steps(self, **values: int) -> None:
        text = self.policy.read_text()
        for key, value in values.items():
            text = text.replace(next(line for line in text.splitlines() if line.startswith(f"{key} =")), f"{key} = {value}")
        self.policy.write_text(text)

    def restore(self, h: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        plan = self.plan(h, {"restore": "accepted"})
        return plan, self.apply_ok(h, plan)

    def assert_expected_words(self) -> None:
        """Every core (each CPU of an E cluster too) holds EXPECTED-words.txt exactly, OC word and every point."""
        for cpu, want in EXPECTED.items():
            unit = TOPO.unit_of_cpu(cpu)
            for c in TOPO.unit_cpus(unit):
                self.assertEqual([f"{w:X}" for w in unit_words(self.sim, c, len(want))], [f"{w:X}" for w in want], f"cpu {c}")


class ColdBoot(CompleteCase):
    def test_one_apply_restores_the_accepted_words_exactly_and_writes_only_differences(self) -> None:
        h = self.boot(COLD, accepted_dir=self.accepted())
        self.read(h)
        plan, result = self.restore(h)
        self.assertTrue(plan["complete"])
        self.assertEqual(plan["rounds"], 1)  # the shipped policy's 200 mV step covers the deepest offset (-91 mV)
        self.assertEqual(plan["ops_needed"], 651)  # worst case reserved: 24 scan reads + 257 reads + 185 writes and their read-backs (the reference layout)
        self.assert_expected_words()
        written = [r for r in self.sim.requests if r.cmd == 0x11]
        self.assertFalse([r for r in written if r.p2 == 0], "an OC word the CPU already holds (cold 0 / 0x39) is never rewritten")
        changed = {(c, p) for cpu, want in EXPECTED.items() for c in TOPO.unit_cpus(TOPO.unit_of_cpu(cpu)) for p, w in enumerate(want[1:], 1) if VfWord.from_u32(w).offset_raw}
        self.assertEqual({(r.cpu, r.p2) for r in written}, changed)  # exactly the points whose offset differs from the cold 0
        self.assertNotIn(0x1D, [r.cmd for r in self.sim.requests])
        again = self.read(h)
        self.assertTrue(all(c["match"] and not c.get("equivalent") for c in again["accepted_match"]), again["accepted_match"])

    def test_a_second_restore_writes_nothing(self) -> None:
        h = self.boot(COLD, accepted_dir=self.accepted())
        self.read(h)
        self.restore(h)
        self.read(h)
        before = len(self.sim.requests)
        plan, result = self.restore(h)
        self.assertEqual(plan["steps"], [])  # the preview lists no write: the last read already holds every word
        self.assertEqual(plan["reads"], 1 + 4 * 24 + 160)  # ... but the apply still reads every word (0x14, 4 per CPU, every point)
        self.assertEqual(result["writes_sent"], 0)
        self.assertFalse([r for r in self.sim.requests[before:] if r.cmd in (0x11, 0x15, 0x1D, 0x27)])

    def test_a_small_step_limit_is_split_into_rounds_in_the_one_apply(self) -> None:
        self.policy_steps(max_offset_step_mv=25)  # -91 mV from the cold 0 is four 25 mV steps
        h = self.boot(COLD, accepted_dir=self.accepted())
        self.read(h)
        plan, result = self.restore(h)
        self.assertGreater(plan["rounds"], 1)
        self.assertTrue(any("guarded rounds" in w for w in texts(plan)), texts(plan))
        self.assertTrue(result["complete"])
        self.assert_expected_words()
        p7 = TOPO.resolve("P", 7).cpu  # 55x -91 mV: the deepest point, reached in steps the guard allows (each judged from the word read in its round)
        self.assertGreaterEqual(len([r for r in self.sim.requests if r.cmd == 0x11 and (r.cpu, r.p2) == (p7, 6)]), 4)

    def test_rounds_that_do_not_fit_the_session_are_refused_before_anything_is_sent(self) -> None:
        self.policy_steps(max_offset_step_mv=25, max_operations_per_session=900)
        h = self.boot(COLD, accepted_dir=self.accepted())
        self.read(h)
        sent = len(self.sim.requests)
        r = h.call("p", "tune.plan", {"intent": {"restore": "accepted"}})
        self.assertEqual((error_code(r), r["error"]["detail"]["check"]), ("REFUSED", "STEP_LIMIT"))
        self.assertIn("rounds", r["error"]["message"])
        self.assertIn("P-Core", r["error"]["message"])  # names the unit and the field that needs the most rounds
        self.assertEqual(len(self.sim.requests), sent)

    def test_without_a_read_the_apply_judges_the_rounds_from_its_own_reads(self) -> None:
        self.policy_steps(max_offset_step_mv=25, max_operations_per_session=900)
        h = self.boot(COLD, accepted_dir=self.accepted())
        plan = self.plan(h, {"restore": "accepted"})  # no read: one round reserved
        r = self.apply(h, plan)
        self.assertEqual((error_code(r), r["error"]["detail"]["check"]), ("REFUSED", "STEP_LIMIT"))
        self.assertFalse([q for q in self.sim.requests if q.cmd in (0x11, 0x15)], "nothing written: the rounds were judged before the first write")


class WarmBoot(CompleteCase):
    def test_a_residual_oc_point_is_neutralised_and_equivalent(self) -> None:
        initial = dict(COLD.initial_vf)
        initial.update({c: VfWord.from_u32(EC1_WORD) for c in EC1})
        h = self.boot(dataclasses.replace(COLD, initial_vf=initial), accepted_dir=self.accepted())
        self.read(h)
        _, result = self.restore(h)
        self.assertTrue(result["residual"], result)
        w = self.sim.vf(12)
        self.assertEqual(w.ratio, 48)  # never a zero ratio: the OC ratio cannot be removed at runtime
        self.assertLessEqual(w.v_target_raw, (self.sim.fused_word(12) >> 8 & 0xFFF) + VfWord.from_u32(EXPECTED[12][6]).offset_raw)
        after = self.read(h)
        ec1 = next(c for c in after["accepted_match"] if c["cpu"] == 12)
        self.assertTrue(ec1["match"] and ec1["equivalent"], ec1)
        self.assertTrue(all(c["match"] for c in after["accepted_match"]), after["accepted_match"])

    def test_a_residual_oc_point_and_a_small_step_limit_in_one_apply(self) -> None:
        self.policy_steps(max_offset_step_mv=25)
        initial = dict(COLD.initial_vf)
        initial.update({c: VfWord.from_u32(EC1_WORD) for c in EC1})
        h = self.boot(dataclasses.replace(COLD, initial_vf=initial), accepted_dir=self.accepted())
        self.read(h)
        plan, result = self.restore(h)
        self.assertGreater(plan["rounds"], 1)
        self.assertTrue(result["residual"], result)
        after = self.read(h)
        self.assertTrue(all(c["match"] for c in after["accepted_match"]), after["accepted_match"])
        self.assertEqual(self.sim.vf(12).ratio, 48)


class SaveAndAccept(CompleteCase):
    def test_save_is_the_read_with_the_edits_over_it(self) -> None:
        h = self.boot(COLD)
        self.read(h)
        r = h.call("s", "profiles.save", {"name": "mine", "intent": rows(row("P3", points={"53": -40}), row("EC2", curve=-10))})
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["result"]["complete"], r["result"]["units"], r["result"]["points"]), (True, 12, 88))
        record = ProfileStore(self.audit.parent / "profiles", euid=__import__("os").geteuid()).load("mine")
        prof = load_profile(record["profile"])
        prof.check_complete(TOPO)
        self.assertEqual({(c.kind, c.index): c.offset_mv for c in prof.entries}[("E", 8)], -10)
        p3 = {p.point: (p.offset_mv, p.expect_ratio) for p in prof.points if (p.kind, p.index) == ("P", 3)}
        self.assertEqual(p3[5], (-40, 53))
        self.assertEqual(p3[6], (0, 55))  # the untouched points are stated: 0
        self.assertEqual({c.fused_top_ratio for c in prof.entries if c.kind == "P"}, {fused_top_ratio(self.sim.fused_word(0))})
        self.assertIsNotNone(prof.turbo)  # both tables of the read
        plan = self.plan(h, {"profile": "mine"})
        self.assertTrue(plan["complete"])
        self.apply_ok(h, plan)
        self.assertEqual(self.word(3, 5).offset_raw, VfWord.mv_to_offset_raw(-40))
        self.assertEqual({self.word(c).offset_raw for c in (16, 17, 18, 19)}, {VfWord.mv_to_offset_raw(-10)})

    def test_save_without_edits_records_the_read(self) -> None:
        h = self.boot(COLD)
        self.read(h)
        r = h.call("s", "profiles.save", {"name": "as-is", "intent": rows()})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["result"]["complete"])

    def test_accepting_an_applied_partial_profile_records_the_complete_state(self) -> None:
        acc = self.tmp / "accepted-parent"
        acc.mkdir()
        h = self.boot(COLD, accepted_dir=acc / "accepted")
        legacy = rows(row("P2", points={"55": -30}))  # a profile saved before G-148: a delta
        ProfileStore(self.audit.parent / "profiles", euid=__import__("os").geteuid()).save("old-p2", legacy)
        self.read(h)
        listed = h.call("l", "profiles.list", {})["result"]["profiles"]
        self.assertIn({"name": "old-p2", "source": "saved", "created": listed[0]["created"], "complete": False}, listed)
        plan = self.plan(h, {"profile": "old-p2"})
        self.assertTrue(any("partial" in w for w in texts(plan)), texts(plan))
        self.apply_ok(h, plan)
        self.read(h)
        r = h.call("a", "profiles.accept", {"name": "old-p2"})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["result"]["complete"])
        current = (acc / "accepted" / "CURRENT").read_text()
        self.assertEqual([line for line in current.splitlines() if line.startswith("profile")], ["profile = profiles/old-p2.toml"])
        prof = load_profile(acc / "accepted" / "profiles" / "old-p2.toml")
        self.assertTrue(prof.complete)
        self.assertEqual(len(prof.entries), 12)
        words = _parse_expected((acc / "accepted" / "EXPECTED-words.txt").read_text())
        p2 = TOPO.resolve("P", 2).cpu
        self.assertEqual(VfWord.from_u32(words[p2][6]).offset_raw, VfWord.mv_to_offset_raw(-30))  # the same words as the profile
        self.assertEqual({p.offset_mv for p in prof.points if (p.kind, p.index, p.point) == ("P", 2, 6)}, {-30})

    def test_accepting_a_partial_profile_that_is_not_applied_is_refused(self) -> None:
        acc = self.tmp / "accepted-parent"
        acc.mkdir()
        h = self.boot(COLD, accepted_dir=acc / "accepted")
        ProfileStore(self.audit.parent / "profiles", euid=__import__("os").geteuid()).save("old", rows(row("P2", points={"55": -30})))
        self.read(h)
        r = h.call("a", "profiles.accept", {"name": "old"})
        self.assertEqual((error_code(r), r["error"]["detail"]["check"]), ("REFUSED", "NOT_APPLIED"))


class AnotherCpu(CompleteCase):
    def test_a_complete_profile_made_on_another_cpu_is_refused(self) -> None:
        acc = self.accepted()
        path = acc / "profiles" / "accepted-2026-10-04.toml"
        path.write_text(path.read_text().replace("fused_top_ratio = 46", "fused_top_ratio = 47").replace("expect_ratio = 46", "expect_ratio = 47"))
        h = self.boot(COLD, accepted_dir=acc)
        self.read(h)
        r = h.call("p", "tune.plan", {"intent": {"restore": "accepted"}})
        self.assertEqual((error_code(r), r["error"]["detail"]["check"]), ("REFUSED", "PROFILE_TOPOLOGY"))
        self.assertIn("another CPU", r["error"]["message"])

    def test_without_a_read_the_planner_refuses_the_write_on_the_live_0x09(self) -> None:
        acc = self.accepted()
        path = acc / "profiles" / "accepted-2026-10-04.toml"
        path.write_text(path.read_text().replace("fused_top_ratio = 57", "fused_top_ratio = 58").replace("expect_ratio = 57", "expect_ratio = 58"))
        h = self.boot(COLD, accepted_dir=acc)
        plan = self.plan(h, {"restore": "accepted"})
        r = self.apply(h, plan)
        self.assertEqual(error_code(r), "REFUSED")
        self.assertFalse([q for q in self.sim.requests if q.cmd == 0x11])

    def test_a_complete_profile_missing_a_unit_is_refused(self) -> None:
        acc = self.accepted()
        path = acc / "profiles" / "accepted-2026-10-04.toml"
        text = path.read_text()
        cut = text.index('[[core]]\nkind = "E"\nindex = 12')
        end = text.index("[[point]]")
        points = [blk for blk in text[end:].split("\n\n") if 'kind = "E"\nindex = 12\n' not in blk]
        path.write_text(text[:cut] + "\n\n".join(points))
        h = self.boot(COLD, accepted_dir=acc)
        r = h.call("p", "tune.plan", {"intent": {"restore": "accepted"}})
        self.assertEqual((error_code(r), r["error"]["detail"]["check"]), ("REFUSED", "PROFILE_TOPOLOGY"))
        self.assertIn("EC3", r["error"]["message"])
