"""G-107 (contract 8 addendum, D-030): heartbeat ``ops_used`` / ``ops_budget`` and the ``vf.dump`` ``accepted_match``. Simulator and temp files only:
no hardware, no /etc, no /var. The meter is driven directly (no timing) except one end-to-end heartbeat check that waits on a condition."""
from __future__ import annotations

import json
import os
import stat
import types
import unittest
from pathlib import Path
from typing import Any

from arltuner.guard import AuditUnreadable, read_state
from arltuner.helper import Helper
from arltuner.helper.ops import ACCEPTED_FILE, BudgetMeter, accepted_match
from arltuner.msr.measured_baseline import FUSED_POINT as REF_FUSED  # the live 0x09 words a tune.read of the simulated reference CPU would have left

from ._support import HelperCase

BOOT = "11111111-2222-3333-4444-555555555555"  # the HelperCase default boot id
OTHER_BOOT = "99999999-8888-7777-6666-555555555555"
TS = "2026-10-08T00:00:00+00:00"


def attempt(boot: str | None, event: str = "attempt") -> str:
    return json.dumps({"ts": TS, "seq": 0, "event": event, "boot_id": boot})


def write_lines(path: Path, lines: list[str], *, newline_at_end: bool = True) -> None:
    path.write_bytes(("\n".join(lines) + ("\n" if newline_at_end else "")).encode("utf-8"))


def append(path: Path, text: str) -> None:
    with open(path, "ab") as handle:
        handle.write(text.encode("utf-8"))


class BudgetMeterTests(HelperCase):
    def helper(self, **overrides: Any) -> Helper:
        return Helper(self.config(**overrides))

    def guard_count(self, boot: str | None = BOOT) -> int:
        return read_state(self.audit).attempts_by_boot.get(boot, 0)

    def test_no_audit_log_is_zero_used_and_the_budget_comes_from_the_policy(self) -> None:
        self.assertEqual(self.helper().budget.snapshot(), (0, 400))  # POLICY_TOML: max_operations_per_boot = 400

    def test_budget_follows_the_loaded_policy_and_its_changes(self) -> None:
        h = self.helper()
        self.assertEqual(h.budget.snapshot()[1], 400)
        self.policy.write_text(self.policy.read_text(encoding="utf-8").replace("max_operations_per_boot = 400", "max_operations_per_boot = 77"), encoding="utf-8", newline="\n")
        self.assertEqual(h.budget.snapshot()[1], 77)
        self.policy.write_text("this is not a policy\n", encoding="utf-8")
        self.assertIsNone(h.budget.snapshot()[1])  # an invalid policy is never guessed
        self.policy.unlink()
        self.assertIsNone(h.budget.snapshot()[1])

    def test_built_in_policy_budget_when_no_policy_file(self) -> None:
        from arltuner.guard import default_m1_policy

        used, budget = self.helper(policy_path=None).budget.snapshot()
        self.assertEqual((used, budget), (0, default_m1_policy().max_operations_per_boot))

    def test_built_in_policy_is_bound_to_the_live_topology_or_gives_no_budget(self) -> None:
        """G-141 (D-036): the no-policy-file budget comes from a policy bound to the live topology; a topology that cannot be derived gives null, not the 285K's number."""
        from arltuner.topology import TopologyError

        def broken() -> Any:
            raise TopologyError("no cpu_core / cpu_atom")

        seen: list[int] = []

        def topology() -> Any:
            from arltuner.topology import synthetic

            seen.append(1)
            return synthetic(6, 8)

        self.assertEqual(self.helper(policy_path=None, topology_fn=topology).budget.snapshot()[1], 12)
        self.assertEqual(seen, [1])  # the helper's own topology function was asked
        self.assertIsNone(self.helper(policy_path=None, topology_fn=broken).budget.snapshot()[1])

    def test_counts_match_the_guard_for_this_boot_only(self) -> None:
        lines = [attempt(BOOT)] * 5 + [attempt(OTHER_BOOT)] * 3 + [attempt(None)] * 2 + [attempt(BOOT, "result"), attempt(BOOT, "refused"), attempt(BOOT, "readback")]
        write_lines(self.audit, lines)
        used, _ = self.helper().budget.snapshot()
        self.assertEqual(used, 5)
        self.assertEqual(used, self.guard_count())

    def test_unknown_boot_id_counts_the_null_bucket_like_the_guard(self) -> None:
        write_lines(self.audit, [attempt(BOOT)] * 4 + [attempt(None)] * 2)
        for boot_fn in (lambda: None, lambda: "  "):
            used, _ = self.helper(boot_id_fn=boot_fn).budget.snapshot()
            self.assertEqual(used, self.guard_count(None))
            self.assertEqual(used, 2)

    def test_incremental_only_new_bytes_are_scanned_and_a_partial_line_waits(self) -> None:
        h = self.helper()
        meter: BudgetMeter = h.budget
        write_lines(self.audit, [attempt(BOOT)] * 3)
        self.assertEqual(meter.snapshot()[0], 3)
        scanned: list[int] = []
        original = meter._scan

        def spy(handle: Any) -> None:
            scanned.append(meter._offset)
            original(handle)

        meter._scan = spy  # type: ignore[method-assign]
        self.assertEqual(meter.snapshot()[0], 3)
        self.assertEqual(scanned, [])  # nothing changed: no read at all
        append(self.audit, attempt(BOOT)[:20])  # a record cut short by a concurrent append
        self.assertEqual(meter.snapshot()[0], 3)
        append(self.audit, attempt(BOOT)[20:] + "\n" + attempt(BOOT) + "\n")
        self.assertEqual(meter.snapshot()[0], 5)
        self.assertEqual(self.guard_count(), 5)
        self.assertEqual(len(scanned), 2)
        self.assertEqual(scanned[1] > 0, True)  # resumed from the end of the last complete line, not from 0

    def test_big_log_is_counted_in_chunks_and_equals_the_guard(self) -> None:
        pad = "x" * 200
        lines = [json.dumps({"ts": TS, "event": "attempt" if i % 3 else "result", "boot_id": BOOT if i % 5 else OTHER_BOOT, "pad": pad}) for i in range(4000)]
        write_lines(self.audit, lines)
        self.assertGreater(self.audit.stat().st_size, 256 * 1024)
        used, _ = self.helper().budget.snapshot()
        self.assertEqual(used, self.guard_count())
        self.assertGreater(used, 0)

    def test_a_replaced_or_truncated_log_is_rescanned(self) -> None:
        h = self.helper()
        write_lines(self.audit, [attempt(BOOT)] * 6)
        self.assertEqual(h.budget.snapshot()[0], 6)
        write_lines(self.audit, [attempt(BOOT)] * 2)  # same inode, shorter
        self.assertEqual(h.budget.snapshot()[0], 2)
        self.audit.unlink()
        write_lines(self.audit, [attempt(BOOT)] * 9)  # new inode
        self.assertEqual(h.budget.snapshot()[0], 9)

    def test_an_audit_record_the_guard_would_refuse_gives_no_count(self) -> None:
        write_lines(self.audit, [attempt(BOOT), "{not json", attempt(BOOT)])
        h = self.helper()
        self.assertIsNone(h.budget.snapshot()[0])
        append(self.audit, attempt(BOOT) + "\n")
        self.assertIsNone(h.budget.snapshot()[0])  # still: the bad line is still in the log
        self.assertEqual(h.budget.snapshot()[1], 400)  # the budget is independent

    def test_an_untrusted_audit_log_gives_no_count(self) -> None:
        target = self.tmp / "elsewhere.jsonl"
        write_lines(target, [attempt(BOOT)] * 4)
        os.symlink(target, self.audit)
        self.assertIsNone(self.helper().budget.snapshot()[0])
        self.audit.unlink()
        write_lines(self.audit, [attempt(BOOT)] * 4)
        self.assertIsNone(self.helper(euid_fn=lambda: os.geteuid() + 1).budget.snapshot()[0])  # foreign owner
        os.chmod(self.audit, 0o666)
        self.assertIsNone(self.helper().budget.snapshot()[0])  # world-writable

    def test_parity_with_read_state_on_crafted_logs(self) -> None:
        """The evidence for 'counted the same way the guard counts': the same bytes go to read_state and to the meter; the meter must give the
        guard's number, or None exactly when the guard raises AuditUnreadable."""
        good = attempt(BOOT)
        no_ts = json.dumps({"event": "attempt", "boot_id": BOOT})
        naive_ts = json.dumps({"ts": "2026-10-08T00:00:00", "event": "attempt", "boot_id": BOOT})
        junk_ts = json.dumps({"ts": "yesterday", "event": "attempt", "boot_id": BOOT})
        int_boot = json.dumps({"ts": TS, "event": "attempt", "boot_id": 7})
        no_boot = json.dumps({"ts": TS, "event": "attempt"})
        not_attempt_bad_ts = json.dumps({"event": "result", "ts": "garbage", "boot_id": BOOT})

        def lines(*items: str) -> bytes:
            return ("\n".join(items) + "\n").encode()

        cases: dict[str, bytes] = {
            "valid": lines(good, attempt(OTHER_BOOT), good, attempt(BOOT, "result")),
            "empty file": b"",
            "only blank lines": b"\n\n  \n",
            "only non-attempt events": lines(attempt(BOOT, "result"), attempt(BOOT, "refused"), not_attempt_bad_ts),
            "bad json mid-file": lines(good, "{not json", good),
            "attempt missing ts": lines(good, no_ts),
            "ts without tz": lines(good, naive_ts),
            "unparsable ts": lines(good, junk_ts),
            "boot_id as int": lines(good, int_boot),
            "boot_id absent counts as null": lines(good, no_boot),
            "non-object line": lines(good, "[1, 2]"),
            "record without event": lines(good, '{"ts": "%s"}' % TS),
            "trailing partial line (cut short)": lines(good, good) + good[:25].encode(),
            "trailing complete record without newline": lines(good, good) + good.encode(),
            "trailing record without newline, bad ts": lines(good) + junk_ts.encode(),
            "carriage return inside a line": (good + "\r\n" + good + "\n").encode(),
            "invalid utf-8 mid-file": good.encode() + b"\n\xff\xfe\n" + good.encode() + b"\n",
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.audit.write_bytes(data)
                try:
                    want: int | None = read_state(self.audit).attempts_by_boot.get(BOOT, 0)
                except AuditUnreadable:
                    want = None
                self.assertEqual(self.helper().budget.snapshot()[0], want)
        # and the same when the log arrives in pieces, the way the heartbeat sees it while a dump appends
        h = self.helper()
        data = cases["valid"] + cases["trailing complete record without newline"]
        self.audit.write_bytes(b"")
        for end in range(0, len(data) + 1, 37):
            self.audit.write_bytes(data[:end])
            try:
                want = read_state(self.audit).attempts_by_boot.get(BOOT, 0)
            except AuditUnreadable:
                want = None
            self.assertEqual(h.budget.snapshot()[0], want, end)

    def test_an_unterminated_giant_line_gives_no_count_and_bounded_memory(self) -> None:
        write_lines(self.audit, [attempt(BOOT)] * 2)
        append(self.audit, "a" * (1024 * 1024 + 4096))  # no newline: never a record this helper wrote
        h = self.helper()
        self.assertIsNone(h.budget.snapshot()[0])
        self.assertLessEqual(len(h.budget._tail[1] or ""), 0)  # nothing was kept as a record
        # a newline-terminated long (but < 1 MiB) record is fine
        write_lines(self.audit, [attempt(BOOT), json.dumps({"ts": TS, "event": "attempt", "boot_id": BOOT, "pad": "b" * 300_000})])
        self.assertEqual(self.helper().budget.snapshot()[0], 2)

    def test_a_same_inode_rewrite_to_a_larger_file_is_rescanned(self) -> None:
        h = self.helper()
        write_lines(self.audit, [attempt(BOOT)] * 3)
        inode = self.audit.stat().st_ino
        self.assertEqual(h.budget.snapshot()[0], 3)
        write_lines(self.audit, [attempt(OTHER_BOOT)] * 10 + [attempt(BOOT)])  # truncate-and-rewrite in place, larger, different head
        self.assertEqual(self.audit.stat().st_ino, inode)
        self.assertGreater(self.audit.stat().st_size, 3 * len(attempt(BOOT)))
        self.assertEqual(h.budget.snapshot()[0], 1)
        self.assertEqual(self.guard_count(), 1)
        append(self.audit, attempt(BOOT) + "\n")  # and appends after the reset are still incremental
        self.assertEqual(h.budget.snapshot()[0], 2)

    def test_heartbeats_carry_the_counts_and_follow_a_dump(self) -> None:
        h = self.start()
        first = h.wait_for(lambda m: m.get("event") == "heartbeat")
        self.assertEqual((first["ops_used"], first["ops_budget"]), (0, 400))
        self.assertTrue({"event", "op_id", "state", "ops_used", "ops_budget"} <= set(first))
        reply = h.call("d1", "vf.dump", {"cpus": "0"})
        self.assertTrue(reply["ok"], reply)
        beat = h.wait_for(lambda m: m.get("event") == "heartbeat" and m.get("ops_used") == reply["result"]["ops_done"], start=0)
        self.assertEqual((beat["ops_used"], beat["ops_budget"]), (10, 400))
        self.assertEqual(beat["ops_used"], self.guard_count())


def fake_lstat(real_dir_mode_extra: int = 0, uid: int = 0) -> Any:
    """lstat that pretends every path is root-owned and not writable by others, so ``check_trusted_path`` can be run on temp paths."""

    def lstat(path: str) -> Any:
        st = os.lstat(path)
        mode = (st.st_mode & ~0o022) | real_dir_mode_extra
        return types.SimpleNamespace(st_mode=mode, st_uid=uid)

    return lstat


def sample_document() -> dict[str, Any]:
    def word(w: int) -> dict[str, Any]:
        return {"word": f"{w:#010x}"}

    return {
        "units": [
            {"cpus": [{"cpu": 1, "oc": word(0), "points": [word(8), word(0x14), word(0xF9A00035)], "end": "x"}]},
            {"cpus": [{"cpu": 2, "oc": word(0), "points": [word(8), word(0x14)], "end": "x"}, {"cpu": 5, "oc": word(7), "points": [word(1)], "end": "x"}]},
        ]
    }


EXPECTED_TEXT = """\
# comment line
1  0 8 14 F9A00035

2  0 8 15
"""


class AcceptedMatchTests(HelperCase):
    def setUp(self) -> None:
        super().setUp()
        self.accepted = self.tmp / "accepted"
        self.accepted.mkdir()
        self.file = self.accepted / ACCEPTED_FILE

    def helper(self, **overrides: Any) -> Helper:
        cfg = self.config(**overrides)
        cfg.accepted_dir = self.accepted  # type: ignore[attr-defined]  # the field G-106 adds to HelperConfig; ops reads it by name
        return Helper(cfg)

    def write(self, text: str, mode: int = 0o644) -> None:
        self.file.write_text(text, encoding="utf-8", newline="\n")
        os.chmod(self.file, mode)

    def test_match_and_mismatch_per_core(self) -> None:
        self.write(EXPECTED_TEXT)
        result, note = accepted_match(self.helper(), sample_document())
        self.assertIsNone(note)
        assert result is not None
        self.assertEqual([c["cpu"] for c in result], [1, 2])  # cpu 5 has no expected line: not listed
        self.assertEqual(result[0], {"cpu": 1, "match": True, "diffs": []})
        self.assertEqual(result[1], {"cpu": 2, "match": False, "diffs": [{"point": 2, "expected": "0x00000015", "actual": "0x00000014"}]})

    def test_a_missing_or_extra_point_is_a_diff_with_null(self) -> None:
        self.write("1 0 8 14 F9A00035 40\n2 0 8\n")
        result, _ = accepted_match(self.helper(), sample_document())
        assert result is not None
        self.assertEqual(result[0]["diffs"], [{"point": 4, "expected": "0x00000040", "actual": None}])
        self.assertEqual(result[1]["diffs"], [{"point": 2, "expected": None, "actual": "0x00000014"}])

    def test_the_oc_word_is_point_zero(self) -> None:
        self.write("1 39 8 14 F9A00035\n")
        result, _ = accepted_match(self.helper(), sample_document())
        assert result is not None
        self.assertEqual(result[0]["diffs"], [{"point": 0, "expected": "0x00000039", "actual": "0x00000000"}])

    def test_an_xtu_shaped_oc_word_equals_the_bare_ratio_word(self) -> None:
        """G-120 fix 5 [MEASURED lnx 2026-10-11]: CPU 0 read 0x5B439 (57x, the fused 0x09 voltage, offset 0) where the accepted state has the cold-boot 0x39."""
        def doc(oc: int) -> dict[str, Any]:
            return {"units": [{"cpus": [{"cpu": 0, "oc": {"word": f"{oc:#010x}"}, "points": [{"word": "0x00000008"}], "end": "x"}]}]}

        self.write("0 39 8\n")
        result, note = accepted_match(self.helper(), doc(0x5B439), REF_FUSED)
        self.assertIsNone(note)
        self.assertEqual(
            result, [{"cpu": 0, "match": True, "diffs": [], "equivalent": True, "equivalents": [{"point": 0, "expected": "0x00000039", "actual": "0x0005b439"}]}]
        )
        self.write("0 5B439 8\n")  # the other direction is as equivalent
        result, _ = accepted_match(self.helper(), doc(0x39), REF_FUSED)
        assert result is not None
        self.assertEqual((result[0]["match"], result[0]["equivalent"]), (True, True))
        for label, actual in (("a runtime offset", 0xFEC5B439), ("another voltage", 0x5B539), ("another ratio", 0x5B438), ("the dropped-target word", 0xFEC00039)):
            with self.subTest(label):
                self.write("0 39 8\n")
                result, _ = accepted_match(self.helper(), doc(actual), REF_FUSED)
                assert result is not None
                self.assertFalse(result[0]["match"])
                self.assertNotIn("equivalent", result[0])
        self.write("0 39 8\n")  # an identical word is a plain match, not an equivalence
        result, _ = accepted_match(self.helper(), doc(0x39), REF_FUSED)
        self.assertEqual(result, [{"cpu": 0, "match": True, "diffs": []}])

    def test_absent_is_null_with_a_note(self) -> None:
        result, note = accepted_match(self.helper(), sample_document())
        self.assertIsNone(result)
        self.assertIn("No accepted state yet", note or "")  # D-036: a fresh install has none

    def test_no_directory_configured_is_null(self) -> None:
        result, note = accepted_match(Helper(self.config()), sample_document())
        self.assertIsNone(result)
        self.assertIn("is configured", note or "")

    def test_symlink_is_untrusted_and_not_followed(self) -> None:
        real = self.tmp / "real.txt"
        real.write_text(EXPECTED_TEXT, encoding="utf-8")
        os.symlink(real, self.file)
        result, note = accepted_match(self.helper(), sample_document())
        self.assertIsNone(result)
        self.assertIn("untrusted", note or "")
        self.assertIn("symlink", note or "")

    def test_hard_link_is_untrusted(self) -> None:
        real = self.tmp / "real.txt"
        real.write_text(EXPECTED_TEXT, encoding="utf-8")
        os.link(real, self.file)
        result, note = accepted_match(self.helper(), sample_document())
        self.assertIsNone(result)
        self.assertIn("hard link", note or "")

    def test_foreign_owner_by_injected_euid_is_untrusted(self) -> None:
        self.write(EXPECTED_TEXT)
        result, note = accepted_match(self.helper(euid_fn=lambda: os.geteuid() + 1), sample_document())
        self.assertIsNone(result)
        self.assertIn("untrusted", note or "")
        self.assertIn("owned by uid", note or "")

    def test_world_or_group_writable_is_untrusted(self) -> None:
        for mode in (0o666, 0o664, 0o646):
            self.write(EXPECTED_TEXT, mode)
            result, note = accepted_match(self.helper(), sample_document())
            self.assertIsNone(result, oct(mode))
            self.assertIn("writable", note or "")

    def test_fifo_and_oversize_and_garbage_are_null(self) -> None:
        os.mkfifo(self.file)
        result, note = accepted_match(self.helper(), sample_document())  # O_NONBLOCK: the open does not hang
        self.assertIsNone(result)
        self.assertIn("untrusted", note or "")
        self.file.unlink()
        self.write("1 0 8\n" + "# pad\n" * 20000)
        self.assertIsNone(accepted_match(self.helper(), sample_document())[0])
        for text in ("", "# only a comment\n", "x 0 8\n", "1 0\n", "1 0 8\n1 0 9\n", "1 0 zz\n", "1 0 1FFFFFFFF\n"):
            self.write(text)
            result, note = accepted_match(self.helper(), sample_document())
            self.assertIsNone(result, repr(text))
            self.assertIn("invalid", note or "", repr(text))

    def test_enforce_trust_checks_the_parent_chain_too(self) -> None:
        self.write(EXPECTED_TEXT)
        # every path pretends to be root-owned and not writable by others; the real file is ours, so the euid check still passes
        ok, note = accepted_match(self.helper(enforce_trust=True, lstat_fn=fake_lstat(), euid_fn=os.geteuid), sample_document())
        self.assertIsNone(note)
        self.assertIsNotNone(ok)
        bad, note = accepted_match(self.helper(enforce_trust=True, lstat_fn=fake_lstat(real_dir_mode_extra=0o020), euid_fn=os.geteuid), sample_document())
        self.assertIsNone(bad)
        self.assertIn("untrusted", note or "")
        foreign, note = accepted_match(self.helper(enforce_trust=True, lstat_fn=fake_lstat(uid=1000), euid_fn=os.geteuid), sample_document())
        self.assertIsNone(foreign)
        self.assertIn("not root", note or "")
        self.file.unlink()
        gone, note = accepted_match(self.helper(enforce_trust=True, lstat_fn=fake_lstat(), euid_fn=os.geteuid), sample_document())
        self.assertIsNone(gone)
        self.assertIn("No accepted state yet", note or "")

    def test_an_implausible_word_count_makes_the_file_untrusted(self) -> None:
        self.write("1 " + " ".join(["F9A00035"] * 17) + "\n")  # 17 words > 16
        result, note = accepted_match(self.helper(), sample_document())
        self.assertIsNone(result)
        self.assertIn("plausible", note or "")
        self.write("1 " + " ".join(["F9A00035"] * 16) + "\n")  # exactly 16 is accepted
        self.assertIsNotNone(accepted_match(self.helper(), sample_document())[0])

    def test_diffs_are_capped_per_cpu_and_in_total(self) -> None:
        def big_document(cpus: int, points: int) -> dict[str, Any]:
            return {"units": [{"cpus": [{"cpu": c, "oc": {"word": "0x00000001"}, "points": [{"word": f"{0x100 + i:#010x}"} for i in range(points)], "end": "x"} for c in range(cpus)]}]}

        self.write("0 2 " + " ".join(f"{0x200 + i:X}" for i in range(14)) + "\n")
        (entry,) = accepted_match(self.helper(), big_document(1, 40))[0] or []
        self.assertEqual((entry["match"], len(entry["diffs"]), entry["truncated"]), (False, 32, True))
        self.assertEqual([d["point"] for d in entry["diffs"]], list(range(32)))
        self.write("0 2 8\n")
        (small,) = accepted_match(self.helper(), big_document(1, 3))[0] or []
        self.assertNotIn("truncated", small)
        # the total across CPUs is bounded too (24 CPUs x 32 would be ~50 KB of diffs)
        self.write("\n".join(f"{c} 2 " + " ".join(f"{0x200 + i:X}" for i in range(14)) for c in range(24)) + "\n")
        listed, _ = accepted_match(self.helper(), big_document(24, 40))
        assert listed is not None
        self.assertEqual(len(listed), 24)
        self.assertLessEqual(sum(len(c["diffs"]) for c in listed), 200)
        self.assertTrue(all(c["match"] is False for c in listed))
        self.assertTrue(all(c.get("truncated") for c in listed[-3:]))

    def test_a_malformed_document_never_raises(self) -> None:
        self.write(EXPECTED_TEXT)
        result, note = accepted_match(self.helper(), {"units": [{"cpus": [{"cpu": 1, "points": [{"word": "zz"}]}]}]})
        self.assertIsNone(result)
        self.assertIn("compare failed", note or "")


class VfDumpAcceptedTests(HelperCase):
    def setUp(self) -> None:
        super().setUp()
        self.accepted = self.tmp / "accepted"
        self.accepted.mkdir()
        self.file = self.accepted / ACCEPTED_FILE

    def started(self):  # type: ignore[no-untyped-def]
        cfg = self.config()
        cfg.accepted_dir = self.accepted  # type: ignore[attr-defined]
        return self.start(cfg)

    def test_dump_against_its_own_words_matches_then_a_changed_word_mismatches(self) -> None:
        h = self.started()
        first = h.call("d1", "vf.dump", {"cpus": "0,1"})
        self.assertTrue(first["ok"], first)
        self.assertIsNone(first["result"]["accepted_match"])  # nothing accepted yet
        self.assertIn("No accepted state yet", first["result"]["accepted_note"])
        lines = []
        for unit in first["result"]["units"]:
            for c in unit["cpus"]:
                lines.append(f"{c['cpu']} " + " ".join(f"{int(w, 16):X}" for w in [c["oc"]["word"]] + [p["word"] for p in c["points"]]))
        self.file.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        os.chmod(self.file, 0o644)
        second = h.call("d2", "vf.dump", {"cpus": "0,1"})
        self.assertTrue(second["ok"], second)
        self.assertEqual([(c["cpu"], c["match"], c["diffs"]) for c in second["result"]["accepted_match"]], [(0, True, []), (1, True, [])])
        self.assertIsNone(second["result"]["accepted_note"])
        # the accepted state now differs from the chip at one point of cpu 1
        parts = lines[1].split()
        old = parts[3]
        parts[3] = "1F" if old != "1F" else "20"
        lines[1] = " ".join(parts)
        self.file.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        third = h.call("d3", "vf.dump", {"cpus": "0,1"})
        self.assertTrue(third["ok"], third)
        by_cpu = {c["cpu"]: c for c in third["result"]["accepted_match"]}
        self.assertTrue(by_cpu[0]["match"])
        self.assertFalse(by_cpu[1]["match"])
        (diff,) = by_cpu[1]["diffs"]
        self.assertEqual(diff["point"], 2)
        self.assertEqual(int(diff["expected"], 16), int(parts[3], 16))
        self.assertEqual(int(diff["actual"], 16), int(old, 16))

    def test_bogus_expected_words_on_all_cpus_never_turn_a_dump_into_internal(self) -> None:
        from arltuner.helper.protocol import MAX_LINE

        h = self.started()
        self.policy.write_text(self.policy.read_text(encoding="utf-8").replace("max_operations_per_boot = 400", "max_operations_per_boot = 2000"), encoding="utf-8", newline="\n")
        # the reviewer's repro: 2,000 bogus words on each of 24 CPUs. Rejected as implausible: null + note, and the dump is a plain success.
        self.file.write_text("".join(f"{c} " + " ".join(["DEADBEEF"] * 2000) + "\n" for c in range(24)), encoding="utf-8", newline="\n")
        os.chmod(self.file, 0o644)
        reply = h.call("d1", "vf.dump", {"cpus": "0-23"}, timeout=60)
        self.assertTrue(reply["ok"], reply)
        self.assertIsNone(reply["result"]["accepted_match"])
        self.assertIn("larger than", reply["result"]["accepted_note"])  # the 64 KiB file cap fires first for this exact repro
        self.assertLess(len(json.dumps(reply, separators=(",", ":"))), MAX_LINE)
        # the same shape under the file cap (24 x 250 words) is stopped by the word-count rule
        self.file.write_text("".join(f"{c} " + " ".join(["DEADBEEF"] * 250) + "\n" for c in range(24)).replace("DEADBEEF", "F00D"), encoding="utf-8", newline="\n")
        reply = h.call("d0", "vf.dump", {"cpus": "0-23"}, timeout=60)
        self.assertTrue(reply["ok"], reply)
        self.assertIsNone(reply["result"]["accepted_match"])
        self.assertIn("plausible", reply["result"]["accepted_note"])
        # the worst case that passes the parser: 16 wrong words per CPU on all 24 CPUs
        self.file.write_text("".join(f"{c} " + " ".join(["DEADBEEF"] * 16) + "\n" for c in range(24)), encoding="utf-8", newline="\n")
        reply = h.call("d2", "vf.dump", {"cpus": "0-23"}, timeout=60)
        self.assertTrue(reply["ok"], reply)
        self.assertLess(len(json.dumps(reply, separators=(",", ":"))), MAX_LINE)
        listed = reply["result"]["accepted_match"]
        self.assertEqual(len(listed), 24)
        self.assertTrue(all(not c["match"] for c in listed))
        self.assertTrue(any(c.get("truncated") for c in listed))

    def test_an_untrusted_accepted_file_never_fails_the_dump(self) -> None:
        h = self.started()
        self.file.write_text("0 0 8\n", encoding="utf-8")
        os.chmod(self.file, 0o666)
        reply = h.call("d1", "vf.dump", {"cpus": "0"})
        self.assertTrue(reply["ok"], reply)
        self.assertIsNone(reply["result"]["accepted_match"])
        self.assertIn("untrusted", reply["result"]["accepted_note"])
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o666)  # read only: never repaired or removed


if __name__ == "__main__":
    unittest.main()


class NeutralOcWordTests(unittest.TestCase):
    """G-124: every neutral OC word (no OC point, offset 0) is equivalent; an offset or an OC point is not [MEASURED lnx 2026-10-12]."""

    def test_neutral_words(self) -> None:
        from arltuner.helper.ops import _equivalent_oc_words as equivalent

        def eq(cpu: int, expected: int, actual: int) -> bool:
            return equivalent(REF_FUSED.get(cpu), expected, actual)

        self.assertTrue(eq(16, 0x0, 0x2E))          # E cold word vs E after an offset-0 whole-curve write
        self.assertTrue(eq(0, 0x39, 0x5B439))       # P0 cold vs XTU shape
        self.assertTrue(eq(1, 0x0, 0x39))           # P1 cold vs bare top ratio, offset 0
        self.assertFalse(eq(0, 0x39, 0x0))          # P0 accepted 0x39 vs a zero word: not shown equivalent
        self.assertFalse(eq(0, 0x38, 0x5B439))      # another ratio
        self.assertFalse(eq(16, 0x0, 0xFEC0002E))   # whole-curve offset -10 mV
        self.assertFalse(eq(12, 0x0, 0x48C30))      # an OC point at 48x (above the fused 46x)
        self.assertFalse(eq(0, 0x39, 0xFEC00039))   # offset on P0
        self.assertFalse(equivalent(None, 0x39, 0x5B439))  # the core's 0x09 word was not read: nothing is judged equivalent
        self.assertTrue(eq(16, 0x0, 0x48C2E))       # G-147: ratio 46 = top with the fused voltage as target (XTU shape, a stale target of a warm reboot): no OC point, same word as 0
        self.assertFalse(eq(16, 0x0, 0x48D2E))      # a target above the fused voltage is not neutral


class ResidualOcPointCompareTests(HelperCase):
    """G-147: a residual OC point (cleared by a cold boot only) that runs the accepted voltages is EQUIVALENT; a real difference stays one."""

    def setUp(self) -> None:
        super().setUp()
        self.accepted = self.tmp / "accepted"
        self.accepted.mkdir()
        self.file = self.accepted / ACCEPTED_FILE

    def helper(self, **overrides: Any) -> Helper:
        cfg = self.config(**overrides)
        cfg.accepted_dir = self.accepted  # type: ignore[attr-defined]
        return Helper(cfg)

    def write(self, text: str, mode: int = 0o644) -> None:
        self.file.write_text(text, encoding="utf-8", newline="\n")
        os.chmod(self.file, mode)

    POINTS = (0x8, 0xF, 0x17, 0x1E, 0x28, 0xFE60002E)  # 46x point at -25 mV (raw -26)
    WANT = "12 0 8 F 17 1E 28 FE60002E\n"

    def doc(self, oc: int, *points: int) -> dict[str, Any]:
        return {"units": [{"cpus": [{"cpu": 12, "oc": {"word": f"{oc:#010x}"}, "points": [{"word": f"{p:#010x}"} for p in points], "end": "x"}]}]}

    def match(self, oc: int, *points: int) -> dict[str, Any]:
        self.write(self.WANT)
        result, note = accepted_match(self.helper(), self.doc(oc, *points), {12: 0x48C2E})
        self.assertIsNone(note)
        assert result is not None
        return result[0]

    def test_neutralised_ec1_is_equivalent_with_the_note_and_the_extra_point(self) -> None:
        entry = self.match(0x46630, *self.POINTS, 0x30)
        self.assertEqual((entry["match"], entry["diffs"], entry["equivalent"]), (True, [], True))
        self.assertEqual([e["point"] for e in entry["equivalents"]], [0, 7])
        self.assertTrue(all(e["residual"] and e["note"] == "residual OC point (cleared by a cold boot); voltages as accepted" for e in entry["equivalents"]))

    def test_the_unneutralised_residual_with_the_high_target_is_still_a_difference(self) -> None:
        entry = self.match(0x48C30, *self.POINTS, 0x30)  # target 1.137 V > the accepted 1.1112 V point
        self.assertFalse(entry["match"])
        self.assertNotIn("equivalent", entry)

    def test_ec2_oc_word_with_the_fused_target_at_the_top_ratio_is_equivalent_to_word_0(self) -> None:
        entry = self.match(0x48C2E, *self.POINTS)
        self.assertEqual((entry["match"], entry["equivalent"]), (True, True))
        self.assertNotIn("residual", entry["equivalents"][0])  # the older kind: no OC point at all

    def test_a_different_offset_or_point_is_not_equivalent(self) -> None:
        for label, oc, points in (("offset", 0xFEC46630, self.POINTS), ("point 6 lost its offset", 0x46630, (*self.POINTS[:-1], 0x2E))):
            with self.subTest(label):
                self.assertFalse(self.match(oc, *points, 0x30)["match"])
