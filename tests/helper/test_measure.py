"""``vf.measure`` / ``curves.*`` / ``mem.read`` through a real helper on pipes, over a fake cpufreq sysfs and the SimModel (no hardware, no process, no sleeping)."""
from __future__ import annotations

import json
import threading
import unittest

from arltuner import vfdump
from arltuner.helper.ops_measure import CAP_ATTR, EPP_ATTR, SimModel
from arltuner.helper.protocol import MAX_LINE, encode

from ._measure_support import ORIGINAL_EPP, MeasureCase
from ._support import error_code


def points(record: dict, unit: str) -> dict[int, dict]:
    return {p["ratio"]: p for p in record["units"][unit]["points"]}


class QuickRunTests(MeasureCase):
    def test_a_quick_run_of_all_twelve_units(self) -> None:
        h = self.start_measure()
        reply = h.call("m", "vf.measure", {"mode": "quick"}, timeout=60)
        self.assertTrue(reply["ok"], reply)
        rec = reply["result"]["record"]
        self.assertEqual(reply["result"]["curve_id"], rec["id"])
        self.assertEqual((rec["mode"], rec["complete"], rec["kernel"], rec["baseline"]), ("quick", True, "6.99-test", False))
        self.assertEqual(rec["tuning_state"], {"kind": "unknown"})
        self.assertEqual(list(rec["units"]), [f"P{n}" for n in range(8)] + [f"EC{n}" for n in range(4)])
        self.assertEqual(rec["units"]["EC2"]["cpus"], [16, 17, 18, 19])
        self.assertEqual([p["ratio"] for p in rec["units"]["P1"]["points"]], [8, 20, 30, 48, 53, 55, 56, 57])
        self.assertEqual([p["ratio"] for p in rec["units"]["EC0"]["points"]], [8, 15, 23, 30, 40, 46, 47, 48])  # F9: + the max turbo bins
        self.assertEqual(set(rec["units"]["P0"]["points"][0]), {"ratio", "mhz", "vid_v", "vid_min", "vid_max", "kept", "total", "status", "fused_point"})  # no reason on an ok point; fused point 1 = 8x
        # 56x / 57x: reachable on the favored P0 and P3 only
        for unit in ("P0", "P3"):
            self.assertEqual({r: points(rec, unit)[r]["status"] for r in (55, 56, 57)}, {55: "ok", 56: "ok", 57: "ok"})
        for unit in ("P1", "P2", "P4", "P5", "P6", "P7"):
            self.assertEqual({r: points(rec, unit)[r]["status"] for r in (55, 56, 57)}, {55: "ok", 56: "unreachable", 57: "unreachable"})
            self.assertIsNone(points(rec, unit)[57]["vid_v"])
            self.assertEqual({r: points(rec, unit)[r]["reason"] for r in (56, 57)}, {56: "max_turbo", 57: "max_turbo"})  # cpuinfo_max_freq 5.5 GHz
        p0 = points(rec, "P0")
        self.assertAlmostEqual(p0[8]["vid_v"], 0.70, delta=0.04)
        self.assertAlmostEqual(p0[57]["vid_v"], 1.40, delta=0.04)
        self.assertTrue(p0[8]["vid_v"] < p0[30]["vid_v"] < p0[48]["vid_v"] < p0[57]["vid_v"])
        self.assertAlmostEqual(points(rec, "EC1")[46]["vid_v"], 1.15, delta=0.04)
        self.assertEqual(p0[30]["mhz"], 3000)
        self.assertTrue(p0[30]["vid_min"] <= p0[30]["vid_v"] <= p0[30]["vid_max"])
        self.assertTrue(5 <= p0[30]["kept"] <= p0[30]["total"] == 10)
        self.assertLess(rec["temps"]["min_c"], rec["temps"]["max_c"])
        # per-core spread: the units do not share one curve
        self.assertGreater(len({points(rec, f"P{n}")[48]["vid_v"] for n in range(8)}), 4)
        self.assertTrue(all(abs(points(rec, f"P{n}")[48]["vid_v"] - p0[48]["vid_v"]) < 0.06 for n in range(8)))
        # stored, and nothing left behind
        self.assertEqual(self.store.get(rec["id"]), rec)
        self.assert_cpufreq_pristine()
        self.assertEqual(self.closed, 1)

    def test_events_progress_and_state(self) -> None:
        h = self.start_measure()
        reply = h.call("m", "vf.measure", {"mode": "quick", "units": ["EC3", "P2"], "name": "mine"}, timeout=60)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["record"]["name"], "mine")
        self.assertEqual(list(reply["result"]["record"]["units"]), ["P2", "EC3"])  # display order, not request order
        pts = [m for m in h.messages if m.get("event") == "vf.point"]
        self.assertEqual(len(pts), 8 + 8)
        self.assertEqual(set(pts[0]), {"event", "op_id", "unit", "ratio", "mhz", "vid_v", "status"})
        self.assertEqual((pts[0]["op_id"], pts[0]["unit"], pts[0]["ratio"], pts[0]["mhz"]), ("m", "P2", 8, 800))
        prog = [m for m in h.messages if m.get("event") == "progress" and m.get("op_id") == "m"]
        self.assertEqual((prog[0]["step"], prog[0]["of"]), (0, 16))
        self.assertEqual((prog[-1]["step"], prog[-1]["of"], prog[-1]["unit"], prog[-1]["ratio"]), (16, 16, "EC3", 48))
        self.assertEqual(set(prog[-1]), {"event", "op_id", "step", "of", "unit", "ratio", "eta_s"})
        self.assertEqual(prog[-1]["eta_s"], 0)
        self.assertGreaterEqual(prog[0]["eta_s"], 1)
        states = [m["state"] for m in h.messages if m.get("event") == "state"]
        self.assertEqual(states[:2], ["measuring", "idle"])

    def test_dense_covers_every_ratio_up_to_the_unit_maximum(self) -> None:
        h = self.start_measure()
        rec = h.call("m", "vf.measure", {"mode": "dense", "units": ["P0", "P1", "EC0"]}, timeout=60)["result"]["record"]
        self.assertEqual([p["ratio"] for p in rec["units"]["P0"]["points"]], list(range(8, 58)))
        self.assertEqual([p["ratio"] for p in rec["units"]["EC0"]["points"]], list(range(8, 49)))
        self.assertEqual({points(rec, "P0")[r]["status"] for r in range(8, 58) if r != 52}, {"ok"})
        self.assertEqual(self.io.read_max_khz(8), 4_900_000)  # cpuinfo_max_freq of an E-core says 49x ...
        self.assertEqual(max(points(rec, "EC0")), 48)  # ... G-118: the 0x650 table says 48, so 49x is never offered
        self.assertEqual(max(points(rec, "P1")), 55)  # dense stops at the unit maximum (cpuinfo_max_freq 5.5 GHz): nothing above is tried
        self.assertEqual(points(rec, "P1")[52]["reason"], "cpufreq_granularity")  # F9: P 52x is not pinnable

    def test_unpinnable_ratios_are_unreachable_with_a_granularity_reason(self) -> None:
        """F9: E 29x (cap 29 -> 28, retry cap 30 -> 30) and E 23x never land on the target: no sample at it => unreachable, not an error and not a mismatch."""
        rec = self.start_measure().call("m", "vf.measure", {"mode": "dense", "units": ["EC0", "P4"]}, timeout=60)["result"]["record"]
        for ratio in (23, 29):
            e = points(rec, "EC0")[ratio]
            self.assertEqual((e["status"], e["reason"], e["vid_v"]), ("unreachable", "cpufreq_granularity", None), ratio)
            self.assertLessEqual(e["kept"], 2)
        self.assertEqual(points(rec, "EC0")[24]["status"], "ok")
        self.assertEqual(points(rec, "EC0")[22]["status"], "ok")
        self.assertEqual(points(rec, "P4")[52]["reason"], "cpufreq_granularity")
        self.assertEqual(points(rec, "P4")[53]["status"], "ok")

    def test_classify_keeps_a_rarely_hit_target_a_mismatch(self) -> None:
        from arltuner.helper.ops_measure import _classify

        hover = vfdump.RatioMeasurement(30, "MISMATCH", 3_000_000, seen_ratios=(29, 30), samples_kept=4, samples_total=10, why="above")
        self.assertEqual(_classify(hover, 46, ()), ("mismatch", None))
        never = vfdump.RatioMeasurement(30, "MISMATCH", 3_000_000, seen_ratios=(29, 31), samples_kept=0, samples_total=10, why="above")
        self.assertEqual(_classify(never, 46, ()), ("unreachable", "cpufreq_granularity"))
        self.assertEqual(_classify(vfdump.RatioMeasurement(57, "skipped", why="below"), 55, ()), ("unreachable", "max_turbo"))

    def test_favored_57x_needs_few_active_cores_and_is_sampled_longer(self) -> None:
        """F3 [MEASURED lnx 2026-10-11]: quiet system => P0/P3 reach 56/57x on ~70 % of the samples (ok, early stop at min_valid, kept < total); with 2+ other busy
        cores the samples never reach it => unreachable, reason active_cores (never an error)."""
        rec = self.start_measure().call("m", "vf.measure", {"mode": "quick", "units": ["P0", "P3"]}, timeout=60)["result"]["record"]
        for unit in ("P0", "P3"):
            for ratio in (56, 57):
                p = points(rec, unit)[ratio]
                self.assertEqual(p["status"], "ok", (unit, ratio, p))
                self.assertTrue(5 <= p["kept"] <= p["total"] <= 40, p)
                self.assertEqual(p["kept"], 5, p)  # the long sampling stops as soon as min_valid samples sit at the target
        self.assertAlmostEqual(points(rec, "P0")[57]["vid_v"], 1.4288, delta=0.004)
        self.assertAlmostEqual(points(rec, "P3")[57]["vid_v"], 1.3964, delta=0.004)
        self.assertEqual(points(rec, "P0")[55]["total"], 10)  # the ordinary points keep 10 samples

    def test_favored_57x_with_two_other_busy_cores_is_unreachable_for_active_cores(self) -> None:
        self.model.background_busy = 2  # a desktop session keeps two more cores awake
        rec = self.start_measure().call("m2", "vf.measure", {"mode": "quick", "units": ["P0"]}, timeout=60)["result"]["record"]
        for ratio in (56, 57):
            p = points(rec, "P0")[ratio]
            self.assertEqual((p["status"], p["reason"], p["vid_v"], p["total"]), ("unreachable", "active_cores", None, 40), p)
        self.assertEqual(points(rec, "P0")[55]["status"], "ok")

    def test_max_ratio_comes_from_the_turbo_tables_not_cpuinfo(self) -> None:
        # a lowered 0x650 table caps the E units; a failed table read falls back to cpuinfo_max_freq (49x) with a note
        self.io.read_msr = lambda cpu, msr: 0x2E2E2E2E2E2E2E2E if msr == 0x650 else 0x3737373737373737  # type: ignore[method-assign]
        rec = self.start_measure().call("m", "vf.measure", {"mode": "dense", "units": ["P1", "EC0"]}, timeout=60)["result"]["record"]
        self.assertEqual((max(points(rec, "EC0")), max(points(rec, "P1"))), (46, 55))

        def broken(cpu: int, msr: int) -> int:
            raise OSError(5, "no msr")

        self.io.read_msr = broken  # type: ignore[method-assign]
        reply = self.start_measure().call("m2", "vf.measure", {"mode": "dense", "units": ["EC0"]}, timeout=60)["result"]
        self.assertEqual(max(points(reply["record"], "EC0")), 49)
        self.assertTrue(any("turbo tables" in p for p in reply["problems"]), reply["problems"])

    def test_p_max_is_the_favored_ratio_of_the_last_tune_read(self) -> None:
        from types import SimpleNamespace

        h = self.start_measure()
        h.helper._tune_state = SimpleNamespace(lock=threading.Lock(), turbo={"favored": {1: 53}}, tables=None)
        rec = h.call("m", "vf.measure", {"mode": "dense", "units": ["P1", "P0"]}, timeout=60)["result"]["record"]
        self.assertEqual((max(points(rec, "P1")), max(points(rec, "P0"))), (53, 57))  # P0 has no entry: cpuinfo_max_freq 5.7 GHz

    def test_a_point_that_missed_is_measured_once_more_and_the_better_attempt_is_kept(self) -> None:
        from unittest import mock

        calls: list[int] = []
        real = vfdump.measure_group

        def spy(*args, **kwargs):  # first call = the whole unit, later calls = the retry pass; the desktop session goes quiet before the retry
            calls.append(len(args[1].ratios))
            if len(calls) == 2:
                self.model.background_busy = 0
            return real(*args, **kwargs)

        self.model.background_busy = 2
        h = self.start_measure()
        with mock.patch.object(vfdump, "measure_group", spy):
            reply = h.call("m", "vf.measure", {"mode": "quick", "units": ["P0"]}, timeout=60)
        self.assertTrue(reply["ok"], reply)
        rec = reply["result"]["record"]
        self.assertEqual(calls, [8, 1, 1])  # 56x and 57x retried singly
        for ratio in (56, 57):
            p = points(rec, "P0")[ratio]
            self.assertEqual((p["status"], p["retried"]), ("ok", True), p)
            self.assertTrue(5 <= p["kept"] <= p["total"], p)  # kept / total are the retry's own
        self.assertNotIn("retried", points(rec, "P0")[55])
        self.assertEqual([(e["ratio"], e["status"]) for e in h.events("vf.point") if e.get("retry")], [(56, "ok"), (57, "ok")])
        last = [e for e in h.events("progress") if e["op_id"] == "m"][-1]
        self.assertEqual((last["step"], last["of"]), (10, 10))

    def test_a_retry_that_misses_again_leaves_the_first_result(self) -> None:
        self.model.background_busy = 2
        h = self.start_measure()
        rec = h.call("m", "vf.measure", {"mode": "quick", "units": ["P0"]}, timeout=60)["result"]["record"]
        for ratio in (56, 57):
            p = points(rec, "P0")[ratio]
            self.assertEqual((p["status"], p["reason"], p["retried"]), ("unreachable", "active_cores", True))
        self.assertEqual([e for e in h.events("vf.point") if e.get("retry")], [])

    def test_baseline_flag_marks_a_complete_record_and_moves(self) -> None:
        h = self.start_measure()
        first = h.call("a", "vf.measure", {"mode": "quick", "units": ["EC0"], "baseline": True}, timeout=60)["result"]["record"]
        self.assertTrue(first["baseline"])
        plain = h.call("b", "vf.measure", {"mode": "quick", "units": ["EC0"]}, timeout=60)["result"]["record"]
        self.assertFalse(plain["baseline"])
        self.assertTrue(self.store.get(first["id"])["baseline"])  # an ordinary run leaves the baseline alone
        second = h.call("c", "vf.measure", {"mode": "quick", "units": ["EC0"], "baseline": True}, timeout=60)["result"]["record"]
        self.assertTrue(second["baseline"])
        self.assertFalse(self.store.get(first["id"])["baseline"])
        self.assertEqual([c["id"] for c in self.store.summaries() if c["baseline"]], [second["id"]])
        self.assertEqual(error_code(h.call("d", "vf.measure", {"baseline": "yes"})), "BAD_REQUEST")

    def test_stock_tuning_state_needs_a_complete_read_with_all_offsets_zero(self) -> None:
        zero = {cpu: [0x39, 0x08, 0x14, 0x1E] for cpu in range(24)}  # ratios only: every offset field 0
        rec = self.start_measure(last_words=zero).call("s", "vf.measure", {"mode": "quick", "units": ["EC0"]}, timeout=60)["result"]["record"]
        self.assertEqual(rec["tuning_state"], {"kind": "stock"})
        tuned = {**zero, 9: [0x39, 0x08 | (0x7F0 << 21)]}  # a raw offset on one point of a non-first E CPU
        rec = self.start_measure(last_words=tuned).call("t", "vf.measure", {"mode": "quick", "units": ["EC0"]}, timeout=60)["result"]["record"]
        self.assertEqual(rec["tuning_state"]["kind"], "words")
        partial = {cpu: ws for cpu, ws in zero.items() if cpu < 8}  # P-cores only: not a complete read
        rec = self.start_measure(last_words=partial).call("u", "vf.measure", {"mode": "quick", "units": ["P0"]}, timeout=60)["result"]["record"]
        self.assertEqual(rec["tuning_state"]["kind"], "words")

    def test_fused_point_labels(self) -> None:
        rec = self.start_measure(last_words={0: [0x39]}).call("f", "vf.measure", {"mode": "dense", "units": ["P0", "P1", "EC0"]}, timeout=60)["result"]["record"]
        p0, p1, ec0 = points(rec, "P0"), points(rec, "P1"), points(rec, "EC0")
        self.assertEqual([p0[r]["fused_point"] for r in (8, 20, 30, 48, 53, 55, 56, 57)], [1, 2, 3, 4, 5, 6, 7, 8])  # P0's OC ratio 57 is also point 8: the index wins
        self.assertNotIn("fused_point", p0[54])  # an in-between ratio carries no label
        self.assertEqual([ec0[r]["fused_point"] for r in (8, 15, 30, 40, 46)], [1, 2, 4, 5, 6])
        self.assertNotIn("fused_point", ec0[47])
        self.assertEqual(ec0[48]["fused_point"], "oc")  # E max 48 is above the top fused point 46: the OC ratio
        self.assertNotIn("fused_point", p1[54])
        from arltuner.helper.ops_measure import _fused_point

        self.assertEqual((_fused_point("P1", 58, 58), _fused_point("P1", 58, None), _fused_point("EC3", 23, None)), ("oc", None, 3))

    def test_other_units_stay_idle_while_one_is_measured(self) -> None:
        seen: list[set[int]] = []
        self.perf_hook = lambda cpu: seen.append(set(self.io.loaded))
        self.start_measure().call("m", "vf.measure", {"mode": "quick", "units": ["P0", "EC1"]}, timeout=60)
        self.assertTrue(all(loaded <= {0} or loaded <= {12, 13, 14, 15} for loaded in seen))

    def test_the_sim_vid_is_the_stock_curve_with_the_f4_clamp(self) -> None:
        """The model's VID = the measured stock curve; an offset at 57x deeper than the 56x voltage is clamped up to it (the measured P0 case)."""
        m = SimModel()
        self.assertAlmostEqual(m.vid("P0", 53), 1.216, places=4)
        self.assertAlmostEqual(m.vid("P0", 54), (1.216 + 1.299) / 2, places=4)  # 54x has no point: the midpoint of 53x / 55x
        self.assertAlmostEqual(m.vid("EC0", 48), 1.157, places=4)  # E 47-48x run on point 6
        m.offsets_mv["P0"] = {57: -49.8}
        self.assertAlmostEqual(m.vid("P0", 57), 1.386, places=4)  # 1.4288 - 0.0498 = 1.379 < V(56x): clamped
        self.assertAlmostEqual(m.vid("P0", 56), 1.386, places=4)

    def test_the_run_writes_only_cap_and_epp_and_journals_first(self) -> None:
        self.start_measure().call("m", "vf.measure", {"mode": "quick", "units": ["P5", "EC1"]}, timeout=60)
        self.assertTrue(self.cpufreq.journal_existed_at_first_write)
        self.assertEqual({attr for _, attr, _ in self.cpufreq.writes}, {CAP_ATTR, EPP_ATTR})
        self.assertEqual({cpu for cpu, _, _ in self.cpufreq.writes}, {5, 12, 13, 14, 15})  # only the selected units' CPUs
        epp = [(cpu, v) for cpu, a, v in self.cpufreq.writes if a == EPP_ATTR]
        self.assertEqual(sorted(epp), sorted([(5, "balance_performance"), *((c, "balance_performance") for c in (12, 13, 14, 15)),
                                              (5, ORIGINAL_EPP), *((c, ORIGINAL_EPP) for c in (12, 13, 14, 15))]))
        self.assert_cpufreq_pristine()

    def test_tuning_state_comes_from_the_words_read_this_session(self) -> None:
        h = self.start_measure(last_words={0: [0x1, 0xDEADBEEF], 3: [0x2]})
        rec = h.call("m", "vf.measure", {"mode": "quick", "units": ["P0"]}, timeout=60)["result"]["record"]
        self.assertEqual(rec["tuning_state"], {"kind": "words", "words": {"0": ["0x00000001", "0xdeadbeef"], "3": ["0x00000002"]}})

    def test_arguments_are_validated(self) -> None:
        h = self.start_measure()
        for n, args in enumerate((
            {"mode": "slow"}, {"units": []}, {"units": ["P0", "P0"]}, {"units": ["P9"]}, {"units": "P0"}, {"name": ""}, {"name": "x" * 65}, {"name": "a\nb"}, {"x": 1}, {"baseline": 1},
        )):
            self.assertEqual(error_code(h.call(f"b{n}", "vf.measure", args)), "BAD_REQUEST", args)
        self.assertEqual(h.helper.machine.state.value, "idle")

    def test_it_is_a_long_exclusive_op_but_not_write_gated(self) -> None:
        gate, entered = threading.Event(), threading.Event()

        def hook(_s: float) -> None:
            entered.set()
            gate.wait(10)

        self.sleep_hook = hook
        h = self.start_measure()
        h.send({"id": "m", "op": "vf.measure", "args": {"mode": "quick", "units": ["P0"]}})
        self.assertTrue(entered.wait(10))
        self.assertEqual(error_code(h.call("d", "vf.dump", {"cpus": "0"})), "BUSY")
        self.assertEqual(error_code(h.call("m2", "vf.measure", {})), "BUSY")
        self.assertEqual(h.call("l", "curves.list")["result"], {"curves": []})  # read-only ops stay available
        self.assertTrue(h.call("p", "preflight")["ok"])
        self.assertEqual(h.helper.machine.state.value, "measuring")
        gate.set()
        self.assertTrue(h.response("m", 30)["ok"])

    def test_no_wiring_is_an_internal_error(self) -> None:
        h = self.start()
        self.assertEqual(error_code(h.call("m", "vf.measure", {})), "INTERNAL")
        self.assertEqual(error_code(h.call("c", "curves.list")), "INTERNAL")
        self.assertEqual(h.helper.machine.state.value, "idle")


class PathsOutTests(MeasureCase):
    def test_cancel_stores_the_partial_and_restores(self) -> None:
        calls = {"n": 0}
        gate, reached = threading.Event(), threading.Event()

        def hook(_s: float) -> None:
            calls["n"] += 1
            if calls["n"] == 25:
                reached.set()
                gate.wait(10)

        self.sleep_hook = hook
        h = self.start_measure()
        h.send({"id": "m", "op": "vf.measure", "args": {"mode": "quick", "name": "cut"}})
        self.assertTrue(reached.wait(10))
        self.assertTrue(self.journal.exists())
        self.assertTrue(self.io.loaded)  # a load is in force right now
        self.assertTrue(h.call("c", "cancel", {"op_id": "m"})["result"]["cancelled"])
        gate.set()
        reply = h.response("m", 30)
        self.assertEqual(error_code(reply), "CANCELLED")
        curve_id = reply["error"]["detail"]["curve_id"]
        partial = reply["error"]["detail"]["partial"]
        self.assertEqual(partial["id"], curve_id)
        self.assertFalse(partial["complete"])
        stored = self.store.get(curve_id)
        self.assertEqual(stored, partial)
        done = sum(len(u["points"]) for u in stored["units"].values())
        self.assertTrue(0 < done < 88, done)
        self.assertEqual(len([m for m in h.messages if m.get("event") == "vf.point"]), done)  # a point cut in half is not reported
        self.assert_cpufreq_pristine()
        self.assertEqual(self.closed, 1)
        self.assertEqual(h.helper.machine.state.value, "idle")

    def test_cancel_before_any_point_stores_nothing(self) -> None:
        gate, reached = threading.Event(), threading.Event()

        def hook(_s: float) -> None:
            reached.set()
            gate.wait(10)

        self.sleep_hook = hook
        h = self.start_measure()
        h.send({"id": "m", "op": "vf.measure", "args": {"units": ["P0"]}})
        self.assertTrue(reached.wait(10))
        h.call("c", "cancel")
        gate.set()
        reply = h.response("m", 30)
        self.assertEqual(error_code(reply), "CANCELLED")
        self.assertNotIn("curve_id", reply["error"].get("detail", {}))
        self.assertEqual(self.store.summaries(), [])
        self.assert_cpufreq_pristine()

    def test_an_exception_restores_and_keeps_the_partial(self) -> None:
        calls = {"n": 0}

        def boom(_cpu: int) -> None:
            calls["n"] += 1
            if calls["n"] == 60:
                raise RuntimeError("msr gone")

        self.perf_hook = boom
        reply = self.start_measure().call("m", "vf.measure", {"mode": "quick", "units": ["P0", "P1"]}, timeout=60)
        self.assertEqual(error_code(reply), "INTERNAL")
        self.assertIn("msr gone", reply["error"]["message"])
        stored = self.store.get(reply["error"]["detail"]["curve_id"])
        self.assertFalse(stored["complete"])
        self.assertTrue(stored["units"]["P0"]["points"])
        self.assert_cpufreq_pristine()
        self.assertEqual(self.closed, 1)

    def test_a_restore_that_fails_is_loud_and_keeps_the_journal(self) -> None:
        h = self.start_measure()
        self.cpufreq.fail = lambda cpu, attr, value: attr == CAP_ATTR and value == str(self.model.max_khz(cpu))  # writing the ORIGINAL cap back fails
        reply = h.call("m", "vf.measure", {"mode": "quick", "units": ["P0"]}, timeout=60)
        self.assertEqual(error_code(reply), "INTERNAL")
        self.assertIn("could NOT restore", reply["error"]["message"])
        self.assertTrue(self.journal.exists(), "an unrestored run keeps its journal for the next start")
        self.assertEqual(self.store.summaries()[0]["id"], reply["error"]["detail"]["curve_id"])  # the measurement itself is kept
        self.cpufreq.fail = None  # the next helper start can finish the job
        from arltuner.helper.ops_measure import recover_journal

        logs: list[str] = []
        self.assertTrue(recover_journal(self.start_measure().helper.measure_wiring, logs.append))
        self.assert_cpufreq_pristine()

    def test_unusable_cpufreq_is_refused_before_anything_changes(self) -> None:
        for cpu in range(24):
            (self.sysfs / f"cpu{cpu}" / "cpufreq" / CAP_ATTR).unlink()
        reply = self.start_measure().call("m", "vf.measure", {}, timeout=30)
        self.assertEqual((error_code(reply), reply["error"]["detail"]["check"]), ("REFUSED", "NO_CPUFREQ"))
        self.assertEqual(self.cpufreq.writes, [])
        self.assertFalse(self.journal.exists())

    def test_dense_without_cpufreq_is_refused_too(self) -> None:
        self.io.read_max_khz = lambda cpu: int((self.sysfs / f"cpu{cpu}" / "cpufreq" / "cpuinfo_max_freq").read_text())  # type: ignore[method-assign]
        for cpu in range(24):
            (self.sysfs / f"cpu{cpu}" / "cpufreq" / "cpuinfo_max_freq").unlink()
        reply = self.start_measure().call("m", "vf.measure", {"mode": "dense"}, timeout=30)
        self.assertEqual((error_code(reply), reply["error"]["detail"]["check"]), ("REFUSED", "NO_CPUFREQ"))
        self.assertEqual(self.cpufreq.writes, [])

    def test_a_load_generator_that_is_not_usable_is_refused(self) -> None:
        self.io.check = lambda: "python3 is not trusted"  # type: ignore[method-assign]
        reply = self.start_measure().call("m", "vf.measure", {}, timeout=30)
        self.assertEqual((error_code(reply), reply["error"]["detail"]["check"]), ("REFUSED", "LOAD_GENERATOR"))
        self.assertEqual(self.cpufreq.writes, [])


class CurvesOpsTests(MeasureCase):
    def run_one(self, h, rid: str, **args) -> dict:
        reply = h.call(rid, "vf.measure", {"mode": "quick", "units": ["P0", "EC0"], **args}, timeout=60)
        self.assertTrue(reply["ok"], reply)
        return reply["result"]["record"]

    def test_list_get_update_delete(self) -> None:
        h = self.start_measure()
        a = self.run_one(h, "m1", name="first")
        b = self.run_one(h, "m2", name="second")
        listing = h.call("l", "curves.list")["result"]["curves"]
        self.assertEqual({r["id"] for r in listing}, {a["id"], b["id"]})
        self.assertEqual(listing, sorted(listing, key=lambda r: r["id"], reverse=True))  # newest first
        row = next(r for r in listing if r["id"] == a["id"])
        self.assertEqual(row, {"id": a["id"], "name": "first", "created": a["created"], "mode": "quick", "complete": True, "tuning_state": {"kind": "unknown"},
                               "units": ["P0", "EC0"], "baseline": False})
        self.assertEqual(h.call("g", "curves.get", {"id": a["id"]})["result"], a)
        only = h.call("g2", "curves.get", {"id": a["id"], "units": ["EC0"]})["result"]
        self.assertEqual(list(only["units"]), ["EC0"])
        up = h.call("u", "curves.update", {"id": a["id"], "name": "  renamed  "})["result"]
        self.assertEqual((up["name"], up["baseline"]), ("renamed", False))
        h.call("u2", "curves.update", {"id": a["id"], "baseline": True})
        h.call("u3", "curves.update", {"id": b["id"], "baseline": True})  # one baseline at most: it moves
        rows = {r["id"]: r["baseline"] for r in h.call("l2", "curves.list")["result"]["curves"]}
        self.assertEqual(rows, {a["id"]: False, b["id"]: True})
        self.assertEqual(h.call("d", "curves.delete", {"id": a["id"]})["result"], {"deleted": a["id"]})
        self.assertEqual([r["id"] for r in h.call("l3", "curves.list")["result"]["curves"]], [b["id"]])
        self.assertEqual(error_code(h.call("d2", "curves.delete", {"id": a["id"]})), "BAD_REQUEST")
        self.assertEqual(error_code(h.call("g3", "curves.get", {"id": a["id"]})), "BAD_REQUEST")

    def test_argument_validation(self) -> None:
        h = self.start_measure()
        bad = [("curves.get", {}), ("curves.get", {"id": "../../etc/passwd"}), ("curves.get", {"id": "x", "units": []}), ("curves.get", {"id": "x", "units": ["Q1"]}),
               ("curves.update", {"id": "20260101T000000Z-abcd"}), ("curves.update", {"id": "20260101T000000Z-abcd", "baseline": 1}),
               ("curves.update", {"id": "20260101T000000Z-abcd", "name": ""}), ("curves.delete", {}), ("curves.list", {"x": 1}), ("mem.read", {"x": 1})]
        for n, (op, args) in enumerate(bad):
            self.assertEqual(error_code(h.call(f"v{n}", op, args)), "BAD_REQUEST", (op, args))
        self.assertEqual(error_code(h.call("n1", "curves.get", {"id": "20260101T000000Z-abcd"})), "BAD_REQUEST")  # well-formed but absent
        self.assertEqual(error_code(h.call("n2", "curves.get", {"id": "x"})), "BAD_REQUEST")  # not an id

    def test_a_dense_record_of_all_units_fits_the_line(self) -> None:
        """The 64 KiB line cap of contract 8: the worst realistic record (dense, 12 units, plus the tuning words of 24 CPUs a ``tune.read`` leaves in ``tuning_state``)
        must travel in one response, whole; anything bigger would be cut by whole units with ``omitted_units``."""
        h = self.start_measure(last_words={cpu: [0x12345678] * 9 for cpu in range(24)})
        reply = h.call("m", "vf.measure", {"mode": "dense"}, timeout=120)
        self.assertTrue(reply["ok"], reply)
        n_points = sum(len(u["points"]) for u in self.store.get(reply["result"]["curve_id"])["units"].values())
        self.assertEqual(n_points, 2 * 50 + 6 * 48 + 4 * 41)
        self.assertLessEqual(len(encode(reply)), MAX_LINE)  # encode() raises when it would not fit
        self.assertNotIn("omitted_units", reply["result"]["record"], "a dense run of all 12 units should still fit one line")
        got = h.call("g", "curves.get", {"id": reply["result"]["curve_id"]})
        self.assertTrue(got["ok"], got)
        record = got["result"]
        shown = sum(len(u["points"]) for u in record["units"].values())
        self.assertEqual(shown + sum(len(self.store.get(record["id"])["units"][u]["points"]) for u in record.get("omitted_units", [])), n_points)
        for u in record.get("omitted_units", []):  # the rest is one curves.get {units} away
            more = h.call(f"g-{u}", "curves.get", {"id": record["id"], "units": [u]})
            self.assertTrue(more["ok"] and u in more["result"]["units"])


class MemOpTests(MeasureCase):
    def test_mem_read_returns_the_wired_reader(self) -> None:
        r = self.start_measure(self.wiring(mem_fn=lambda: {"available": False, "reason": "no"})).call("m", "mem.read")
        self.assertEqual(r["result"], {"available": False, "reason": "no"})

    def test_sim_fixture(self) -> None:
        r = self.start_measure().call("m", "mem.read")["result"]
        self.assertEqual((r["available"], r["mts"], r["gear"], r["cmd_rate"], r["timings"]["tCL"], r["timings"]["tRFC"]), (True, 7200, 2, "2T", 36, 574))
        self.assertEqual(len(r["dimms"]), 2)

    def test_mem_read_and_curves_are_not_write_gated(self) -> None:
        from ._support import UNKNOWN_REPORT

        h = self.start_measure(self.wiring())
        h.helper.cfg.simulator = False  # a hardware-style helper: write ops would need a fresh preflight in mode write
        h.helper.cfg.preflight_fn = lambda: UNKNOWN_REPORT
        self.assertTrue(h.call("m", "mem.read")["ok"])
        self.assertTrue(h.call("c", "curves.list")["ok"])


if __name__ == "__main__":
    unittest.main()
