"""G-113: the fake helper speaks Contract 9 (SPEC-v3) with realistic fixtures, so the page can be built and shown without G-111 / G-112.
These tests pin the fake's behaviour (shapes the page binds to), over the same HelperLink the server uses. No hardware, no real helper code."""
from __future__ import annotations

import queue
import re
import threading
import time
import unittest

from tests.web.fake_helper import FakeHelper
from tests.web import fake_v3 as v3


class V3Base(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeHelper()
        self.addCleanup(self.fake.close)
        self.sub = self.fake.link.subscribe()
        self.addCleanup(self.sub.close)

    def ok(self, op: str, args: dict | None = None, timeout: float = 10.0) -> dict:
        r = self.fake.link.call(op, args or {}, timeout=timeout)
        self.assertTrue(r.get("ok"), r)
        return r["result"]

    def err(self, op: str, args: dict | None = None, code: str | None = None) -> dict:
        r = self.fake.link.call(op, args or {}, timeout=10.0)
        self.assertFalse(r.get("ok"), r)
        if code:
            self.assertEqual(r["error"]["code"], code, r)
        return r["error"]

    def events(self, name: str) -> list[dict]:
        """Everything the helper emitted so far (kept in ``self.seen``), filtered by event name; each name is returned once."""
        seen = self.__dict__.setdefault("seen", [])
        while True:
            try:
                ev = self.sub.queue.get(timeout=0.2)
            except queue.Empty:
                break
            if isinstance(ev, dict):
                seen.append(ev)
        out = [e for e in seen if e.get("event") == name]
        seen[:] = [e for e in seen if e.get("event") != name]
        return out


class TestMemory(V3Base):
    def test_fixture_is_the_measured_ddr5_7200(self):
        r = self.ok("mem.read")
        self.assertTrue(r["available"])
        self.assertEqual((r["mts"], r["gear"], r["cmd_rate"]), (7200, 2, "2T"))
        t = r["timings"]
        self.assertEqual([t[k] for k in ("tCL", "tRCD", "tRP", "tRAS")], [36, 44, 44, 96])
        self.assertEqual((t["tRC"], t["tRFC"], t["tFAW"]), (140, 574, 32))
        self.assertEqual(r["turnaround"]["RDRD"], {"SG": 18, "DG": 8, "SD": 16, "DD": 16})
        self.assertEqual(r["turnaround"]["WRRD"], {"SG": 68, "DG": 48, "SD": 13, "DD": 13})
        self.assertEqual(r["max_bandwidth_mbs"], 115200)
        self.assertEqual(len(r["dimms"]), 2)
        self.assertEqual(r["dimms"][0]["temp_c"]["status"], "ok")
        self.assertTrue(r["cross_check"]["agree"])

    def test_unavailable_carries_a_reason(self):
        self.fake.memory = v3.memory_fixture(available=False)
        r = self.ok("mem.read")
        self.assertFalse(r["available"])
        self.assertIn("MCHBAR", r["reason"])


class TestCurves(V3Base):
    def test_store_list_get_update_delete(self):
        a = self.fake.seed_curve("Stock baseline", baseline=True, offset_mv=0.0, created="2026-10-07T20:41:09Z")
        b = self.fake.seed_curve("tuned", created="2026-10-09T18:02:44Z")
        rows = self.ok("curves.list")["curves"]
        self.assertEqual([r["name"] for r in rows], ["tuned", "Stock baseline"])  # newest first
        self.assertTrue(all(re.fullmatch(v3.CURVE_ID, r["id"]) for r in rows))
        self.assertEqual(rows[1]["baseline"], True)
        self.assertEqual(rows[0]["tuning_state"], {"kind": "unknown"})
        self.assertEqual(len(rows[0]["units"]), 12)
        rec = self.ok("curves.get", {"id": b["id"]})
        self.assertEqual(set(rec["units"]), set(v3.UNITS))
        self.assertEqual(rec["units"]["EC1"]["cpus"], [12, 13, 14, 15])
        p = rec["units"]["P0"]["points"][0]
        self.assertEqual(set(p), {"ratio", "mhz", "vid_v", "vid_min", "vid_max", "kept", "total", "status"})
        # one baseline at most; rename; delete
        self.ok("curves.update", {"id": b["id"], "baseline": True})
        flags = {r["name"]: r["baseline"] for r in self.ok("curves.list")["curves"]}
        self.assertEqual(flags, {"tuned": True, "Stock baseline": False})
        self.ok("curves.update", {"id": b["id"], "name": "renamed"})
        self.assertEqual(self.ok("curves.get", {"id": b["id"]})["name"], "renamed")
        self.ok("curves.delete", {"id": a["id"]})
        self.err("curves.get", {"id": a["id"]})
        self.assertEqual(len(self.ok("curves.list")["curves"]), 1)

    def test_synthetic_curves_span_the_expected_voltages(self):
        rec = self.fake.seed_curve("x", offset_mv=0.0, mode="dense")
        p0 = [p for p in rec["units"]["P0"]["points"] if p["status"] == "ok"]
        self.assertEqual((p0[0]["ratio"], p0[-1]["ratio"]), (8, 57))
        self.assertAlmostEqual(p0[0]["vid_v"], 0.70, delta=0.04)
        self.assertAlmostEqual(p0[-1]["vid_v"], 1.40, delta=0.04)
        e0 = rec["units"]["EC0"]["points"]
        self.assertAlmostEqual(e0[0]["vid_v"], 0.65, delta=0.04)
        self.assertAlmostEqual(e0[-1]["vid_v"], 1.15, delta=0.04)
        self.assertEqual((len(rec["units"]["P0"]["points"]), len(rec["units"]["P1"]["points"])), (50, 48))  # 8..57 and 8..55


class TestMeasure(V3Base):
    def test_quick_run_emits_points_and_stores_the_record(self):
        res = self.ok("vf.measure", {"mode": "quick", "name": "t1"})
        pts = self.events("vf.point")
        prog = self.events("progress")
        self.assertEqual(len(pts), 8 * 8 + 4 * 6)
        self.assertEqual(len(prog), len(pts))
        self.assertEqual(set(prog[-1]), {"event", "op_id", "step", "of", "unit", "ratio", "eta_s"})
        self.assertEqual((prog[-1]["step"], prog[-1]["of"]), (88, 88))
        self.assertEqual({e["op_id"] for e in pts}, {e["op_id"] for e in prog})
        self.assertEqual(set(pts[0]), {"event", "op_id", "unit", "ratio", "mhz", "vid_v", "status"})
        rec = res["record"]
        self.assertEqual(res["curve_id"], rec["id"])
        self.assertEqual((rec["name"], rec["mode"], rec["complete"]), ("t1", "quick", True))
        self.assertEqual(rec["tuning_state"], {"kind": "unknown"})  # tune.read has not happened this boot
        # non-favored P-cores cannot reach 56 / 57: unreachable, not an error, no voltage
        p1 = {p["ratio"]: p for p in rec["units"]["P1"]["points"]}
        self.assertEqual((p1[56]["status"], p1[56]["vid_v"]), ("unreachable", None))
        self.assertEqual(p1[55]["status"], "ok")
        p0 = {p["ratio"]: p for p in rec["units"]["P0"]["points"]}
        self.assertEqual(p0[57]["status"], "ok")
        self.assertEqual(self.ok("curves.list")["curves"][0]["id"], rec["id"])

    def test_selection_measures_only_those_units(self):
        res = self.ok("vf.measure", {"mode": "quick", "units": ["P3", "EC2"]})
        self.assertEqual(set(res["record"]["units"]), {"P3", "EC2"})
        self.assertEqual({e["unit"] for e in self.events("vf.point")}, {"P3", "EC2"})

    def test_cancel_keeps_the_partial_record(self):
        self.fake.measure_delay = 0.02
        got: dict = {}
        done = threading.Event()

        def cb(rid, resp):
            got["rid"], got["resp"] = rid, resp
            done.set()

        rid = self.fake.link.submit("vf.measure", {"mode": "quick"}, cb)
        time.sleep(0.3)
        self.assertTrue(self.ok("cancel", {"op_id": rid})["cancelled"])
        self.assertTrue(done.wait(5))
        self.assertEqual(got["resp"]["error"]["code"], "CANCELLED")
        detail = got["resp"]["error"]["detail"]
        self.assertEqual(set(detail), {"state", "curve_id"})  # scalars only: the server strips anything else
        rec = self.ok("curves.get", {"id": detail["curve_id"]})
        self.assertFalse(rec["complete"])
        listed = self.ok("curves.list")["curves"]
        self.assertEqual([(c["id"], c["complete"]) for c in listed], [(rec["id"], False)])
        self.assertLess(sum(len(u["points"]) for u in rec["units"].values()), 88)

    def test_a_measurement_sees_the_offsets_in_force(self):
        stock = self.fake.seed_curve("stock", offset_mv=0.0)["units"]["P0"]["points"][3]["vid_v"]
        now = self.ok("vf.measure", {"mode": "quick", "units": ["P0"]})["record"]["units"]["P0"]["points"][3]["vid_v"]
        self.assertAlmostEqual(stock - now, 0.025, places=3)  # booted at P -25 mV


class TestG111Deviations(V3Base):
    def test_big_records_come_back_in_pieces(self):
        self.fake.record_budget_units = 5
        res = self.ok("vf.measure", {"mode": "quick"})
        rec = res["record"]
        self.assertEqual(len(rec["units"]), 5)
        self.assertEqual(rec["omitted_units"], v3.UNITS[5:])
        got = self.ok("curves.get", {"id": res["curve_id"]})
        self.assertEqual(got["omitted_units"], v3.UNITS[5:])
        rest = self.ok("curves.get", {"id": res["curve_id"], "units": got["omitted_units"][:5]})
        self.assertEqual(list(rest["units"]), v3.UNITS[5:10])
        last = self.ok("curves.get", {"id": res["curve_id"], "units": ["EC3"]})
        self.assertEqual(list(last["units"]), ["EC3"])
        self.assertNotIn("omitted_units", last)

    def test_problems_and_refusal_checks(self):
        self.fake.measure_problems = ["EC2: 1 of 6 points mismatched"]
        self.assertEqual(self.ok("vf.measure", {"mode": "quick", "units": ["EC2"]})["problems"], ["EC2: 1 of 6 points mismatched"])
        self.fake.measure_refusal = {"code": "REFUSED", "message": "no cpufreq", "check": "NO_CPUFREQ"}
        self.assertEqual(self.err("vf.measure", {"mode": "quick"}, "REFUSED")["detail"]["check"], "NO_CPUFREQ")
        self.assertEqual(self.err("curves.get", {"id": "20260101T000000Z-aaaa"})["detail"]["check"], "NO_SUCH_CURVE")

    def test_update_returns_the_list_row(self):
        c = self.fake.seed_curve("x")
        row = self.ok("curves.update", {"id": c["id"], "name": "y"})
        self.assertEqual(set(row), {"id", "name", "created", "mode", "complete", "tuning_state", "units", "baseline"})
        self.assertEqual(row["tuning_state"], {"kind": "unknown"})


class TestTune(V3Base):
    """The G-112 shapes: tune.read adds units[].id, tune (decoded), tables; before / after are {unit: {"0": OC word, "<n>": point n}} of the words written."""

    def read(self) -> dict:
        return self.ok("tune.read")

    def intent(self, **row) -> dict:
        return {"intent": {"scope": "simple" if row["target"] in ("P", "E") else "unit", "rows": [{"offset": None, "v_target_v": None, "ratio": None, **row}]}}

    def test_read_adds_ids_decoded_words_and_turbo_tables(self):
        r = self.read()
        self.assertTrue(r["complete"] and r["simulator"])
        self.assertEqual([u["id"] for u in r["units"]], v3.UNITS)
        u0, e0 = r["units"][0], r["units"][8]
        self.assertEqual((u0["kind"], u0["cluster"], u0["cluster_cpus"]), ("P", None, [0]))
        self.assertEqual((e0["kind"], e0["cluster"], e0["cluster_cpus"]), ("E", 0, [8, 9, 10, 11]))
        pts = r["tune"]["P0"]["cpus"][0]["points"]
        self.assertEqual([p["point"] for p in pts], list(range(9)))                       # point 0 is the OC word
        self.assertEqual(set(pts[1]), {"point", "ratio", "offset_mv", "v_target_v", "mode"})
        self.assertEqual([p["ratio"] for p in pts[1:]], v3.P_POINTS_FAVORED)
        self.assertEqual(pts[0]["ratio"], 57)
        self.assertIsNotNone(pts[0]["v_target_v"])
        oc_e = r["tune"]["EC0"]["cpus"][0]["points"][0]
        self.assertEqual((oc_e["ratio"], oc_e["v_target_v"]), (None, None))                # unset: the word reads 0
        self.assertEqual([p["ratio"] for p in r["tune"]["P1"]["cpus"][0]["points"][1:]], v3.P_POINTS_OTHER)  # per-core tables
        self.assertEqual(r["tables"], {"p": [57, 57, 55, 55, 55, 55, 55, 55], "e": [48, 48, 47, 46, 46, 46, 46, 46]})
        self.assertIsNone(r["tables_note"])
        self.assertTrue(all(m["match"] for m in r["accepted_match"]))

    def test_plan_lists_only_the_words_it_writes(self):
        self.read()
        p = self.ok("tune.plan", self.intent(target="P", offset={"kind": "curve", "mv": -40}))
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", p["plan_id"]))
        self.assertTrue(re.fullmatch(r"[A-Z2-9]{6}", p["nonce"]))
        self.assertEqual(p["units"], v3.UNITS[:8])
        self.assertEqual(list(p["before"]["P0"]), ["0"])
        self.assertEqual(p["before"]["P0"]["0"]["offset_mv"], -25.0)
        self.assertEqual(p["after"]["P0"]["0"]["offset_mv"], -40.0)
        self.assertNotIn("EC0", p["after"])                         # only the units it changes
        self.assertEqual((p["writes"], p["reads"]), (len(p["steps"]) - 1, 8))
        self.assertEqual(p["steps"][0], {"kind": "write_percore_enable", "conditional": True})
        self.assertFalse(p["words_assumed"])
        self.assertGreater(p["ops_needed"], 0)
        self.assertTrue(any("Revert" in w for w in p["warnings"]))
        pts = self.ok("tune.plan", self.intent(target="EC1", offset={"kind": "points", "mv": {"23": -80}}))
        self.assertEqual(list(pts["after"]["EC1"]), ["3"])           # point 3 is 23x
        self.assertEqual(pts["after"]["EC1"]["3"], {"ratio": 23, "offset_mv": -80.0, "v_target_v": None})
        w = self.ok("tune.plan", self.intent(target="P3", offset={"kind": "curve", "mv": 10}, v_target_v=1.5))
        self.assertTrue(any("positive offset on P3" in x for x in w["warnings"]))
        self.assertTrue(any("target above fused on P3" in x for x in w["warnings"]))

    def test_plan_refusals_names_and_checks(self):
        self.read()
        for row, field in (({"offset": {"kind": "curve", "mv": 201}}, "offset.mv"), ({"v_target_v": 1.7}, "v_target_v"), ({"v_target_v": 0.8}, "v_target_v"),
                           ({"ratio": 58}, "ratio")):
            e = self.err("tune.plan", {"intent": {"scope": "unit", "rows": [{"target": "P2", "offset": None, "v_target_v": None, "ratio": None, **row}]}}, "REFUSED")
            self.assertEqual(e["detail"]["check"], "POLICY_RANGE")
            self.assertIn(field, e["message"])
        self.assertIn("1.355", self.err("tune.plan", self.intent(target="E", v_target_v=1.4), "REFUSED")["message"])
        self.assertEqual(self.err("tune.plan", self.intent(target="P"), "BAD_REQUEST")["detail"]["check"], "NOTHING_TO_DO")
        self.ok("tune.plan", self.intent(target="P", offset={"kind": "curve", "mv": -200}, v_target_v=1.625, ratio=57))  # the limits themselves are fine
        # a group target is only valid in the simple scope
        self.err("tune.plan", {"intent": {"scope": "unit", "rows": [{"target": "P", "offset": {"kind": "curve", "mv": -5}, "v_target_v": None, "ratio": None}]}}, "BAD_REQUEST")

    def test_per_point_offsets_and_reset_need_a_read_first(self):
        for intent in ({"scope": "unit", "rows": [{"target": "P1", "offset": {"kind": "points", "mv": {"20": -30}}, "v_target_v": None, "ratio": None}]},
                       {"reset": "stock"}):
            self.assertEqual(self.err("tune.plan", {"intent": intent}, "REFUSED")["detail"]["check"], "NO_READ")
        # a whole-curve offset needs no read; the word was not read, so its "before" is null and the plan says the words were assumed
        p = self.ok("tune.plan", self.intent(target="P", offset={"kind": "curve", "mv": -30}))
        self.assertIsNone(p["before"]["P0"]["0"])
        self.assertTrue(p["words_assumed"])

    def test_apply_needs_the_exact_nonce_once_reads_back_with_cpus_and_goes_stale(self):
        self.read()
        p = self.ok("tune.plan", self.intent(target="P", offset={"kind": "curve", "mv": -40}))
        other = self.ok("tune.plan", self.intent(target="E", offset={"kind": "curve", "mv": -60}))
        self.err("tune.apply", {"plan_id": p["plan_id"], "typed": "CONFIRM AAAAAA"}, "CONFIRM_MISMATCH")
        self.err("tune.apply", {"plan_id": "0" * 64, "typed": "CONFIRM " + p["nonce"]}, "PLAN_STALE")
        res = self.ok("tune.apply", {"plan_id": p["plan_id"], "typed": "CONFIRM " + p["nonce"]})
        self.assertTrue(res["complete"])
        self.assertEqual(len(res["readback"]), 8)                       # the 8 OC words (a whole-curve offset is the OC word)
        self.assertTrue(all(x["match"] and x["expected"] == x["actual"] for x in res["readback"]))
        self.assertEqual(set(res["readback"][0]), {"unit", "point", "cpu", "expected", "actual", "match"})
        self.assertEqual((res["writes_sent"], res["readback"][0]["cpu"]), (8, 0))
        self.err("tune.apply", {"plan_id": p["plan_id"], "typed": "CONFIRM " + p["nonce"]}, "CONFIRM_MISMATCH")  # a nonce is single-use
        self.err("tune.apply", {"plan_id": other["plan_id"], "typed": "CONFIRM " + other["nonce"]}, "PLAN_STALE")  # a word changed since it was planned
        again = self.read()
        self.assertEqual(again["tune"]["P0"]["cpus"][0]["points"][1]["offset_mv"], -40.0)
        self.assertFalse(again["accepted_match"][0]["match"])
        self.assertTrue(again["accepted_match"][8]["match"])
        self.assertTrue(self.events("progress"))

    def test_three_wrong_nonces_drop_the_plan(self):
        self.read()
        p = self.ok("tune.plan", self.intent(target="P", offset={"kind": "curve", "mv": -40}))
        for _ in range(3):
            self.err("tune.apply", {"plan_id": p["plan_id"], "typed": "CONFIRM AAAAAA"}, "CONFIRM_MISMATCH")
        self.err("tune.apply", {"plan_id": p["plan_id"], "typed": "CONFIRM " + p["nonce"]}, "PLAN_STALE")

    def test_a_partial_apply_is_a_result_with_a_stop(self):
        self.read()
        self.fake.apply_stop_after = 2
        p = self.ok("tune.plan", self.intent(target="P", offset={"kind": "curve", "mv": -30}))
        res = self.ok("tune.apply", {"plan_id": p["plan_id"], "typed": "CONFIRM " + p["nonce"]})
        self.assertFalse(res["complete"])
        self.assertEqual(set(res["stop"]), {"kind", "reason", "tripped"})
        self.assertEqual((res["writes_sent"], len(res["readback"])), (2, 2))

    def test_a_readback_mismatch_and_a_raised_turbo_table(self):
        self.read()
        self.fake.apply_mismatch = {("P0", 0)}
        p = self.ok("tune.plan", self.intent(target="P0", ratio=57, v_target_v=1.4))
        rb = self.ok("tune.apply", {"plan_id": p["plan_id"], "typed": "CONFIRM " + p["nonce"]})["readback"]
        self.assertEqual([(x["unit"], x["point"]) for x in rb if not x["match"]], [("P0", 0)])
        self.fake.tables["p"][0] = 55          # a machine whose P table tops out below the request
        raised = self.ok("tune.plan", self.intent(target="P1", ratio=57))
        self.assertEqual(raised["steps"][-1], {"kind": "write_msr", "table": "p"})
        res = self.ok("tune.apply", {"plan_id": raised["plan_id"], "typed": "CONFIRM " + raised["nonce"]})
        self.assertEqual(res["readback"][-1]["unit"], "P")
        self.assertEqual(res["readback"][-1]["point"], "turbo")  # turbo rows: unit "E" | "P", point "turbo"
        self.assertEqual(len(res["readback"][-1]["expected"]), 18)

    def test_reset_zeroes_offsets_only_and_restore_goes_through_the_same_flow(self):
        self.read()
        reset = self.ok("tune.plan", {"intent": {"reset": "stock"}})
        self.assertEqual(sorted(reset["after"]), sorted(v3.UNITS))
        self.assertTrue(all(w["offset_mv"] == 0 for u in reset["after"].values() for w in u.values()))
        self.assertEqual(reset["after"]["P0"]["0"]["ratio"], 57)         # ratio and V target stay
        self.ok("tune.apply", {"plan_id": reset["plan_id"], "typed": "CONFIRM " + reset["nonce"]})
        restore = self.ok("tune.plan", {"intent": {"restore": "accepted"}})
        self.assertEqual(restore["after"]["P0"]["1"]["offset_mv"], -25.0)
        self.assertEqual(restore["before"]["P0"]["1"]["offset_mv"], 0.0)
        self.assertEqual(self.err("tune.plan", {"intent": {"profile": "nope"}}, "REFUSED")["detail"]["check"], "PROFILE_NOT_FOUND")

    def test_the_measured_record_knows_the_tuning_state(self):
        self.read()
        self.assertEqual(self.ok("vf.measure", {"mode": "quick", "units": ["P0"]})["record"]["tuning_state"]["kind"], "words")
        self.assertEqual(self.ok("curves.list")["curves"][0]["tuning_state"], {"kind": "words"})


class TestProfilesAndSampler(V3Base):
    def test_profiles_list_save_never_overwrite_and_plan_by_name(self):
        self.assertEqual([p["name"] for p in self.ok("profiles.list")["profiles"]], ["accepted"])
        intent = {"scope": "simple", "rows": [{"target": "E", "offset": {"kind": "curve", "mv": -60}, "v_target_v": None, "ratio": None}]}
        self.ok("profiles.save", {"name": "e-minus-60", "intent": intent})
        rows = {p["name"]: p for p in self.ok("profiles.list")["profiles"]}
        self.assertEqual((rows["e-minus-60"]["source"], rows["accepted"]["source"]), ("saved", "accepted"))
        self.assertEqual(rows["e-minus-60"]["intent"], intent)
        e = self.err("profiles.save", {"name": "e-minus-60", "intent": intent}, "REFUSED")
        self.assertEqual(e["detail"]["check"], "EXISTS")
        self.err("profiles.save", {"name": "Bad Name", "intent": intent}, "BAD_REQUEST")
        plan = self.ok("tune.plan", {"intent": {"profile": "e-minus-60"}})
        self.assertEqual(plan["after"]["EC3"]["0"]["offset_mv"], -60.0)

    def test_telemetry_start_restarts_on_a_new_interval(self):
        a = self.ok("telemetry.start", {"interval_s": 1})
        self.assertEqual((a["restarted"], a["already_running"]), (False, False))
        b = self.ok("telemetry.start", {"interval_s": 1})
        self.assertEqual((b["restarted"], b["already_running"]), (False, True))
        c = self.ok("telemetry.start", {"interval_s": 0.25})
        self.assertEqual((c["restarted"], c["interval_s"]), (True, 0.25))

    def test_heartbeat_state_follows_the_running_op(self):
        self.assertEqual(self.fake.busy_state(), ("idle", None))
        self.fake.measure_delay = 0.05
        rid = self.fake.link.submit("vf.measure", {"mode": "quick"}, lambda r, x: None)
        time.sleep(0.2)
        self.assertEqual(self.fake.busy_state(), ("measuring", rid))
        self.ok("cancel", {"op_id": rid})
        time.sleep(0.3)
        self.assertEqual(self.fake.busy_state(), ("idle", None))


if __name__ == "__main__":
    unittest.main()
