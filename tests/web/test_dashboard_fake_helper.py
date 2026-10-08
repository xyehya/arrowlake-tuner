"""G-105: the fake helper speaks the Contract 8 G-107 addendum, and the server passes those shapes to the dashboard unchanged
(heartbeat budget fields, vf.dump 202 + SSE result, ``accepted_match``). No browser: the page's logic is exercised against the same wire shapes."""
from __future__ import annotations

import json
import socket
import time

from tests.web.fake_helper import preflight_report, telemetry_values, vf_dump_document
from tests.web.test_server_security import Base, open_sse, read_until


class Sse:
    """A tiny SSE reader on the raw socket the existing tests use: ``wait(name, pred)`` returns the first matching event's data."""

    def __init__(self, tc: Base, cookie: str) -> None:
        self.sock = open_sse(tc, cookie)
        head = read_until(self.sock, b"retry: 3000\n\n")  # may already hold the first events (replay): keep what follows the retry line
        self.buf = head.split(b"retry: 3000\n\n", 1)[1] if b"retry: 3000\n\n" in head else b""
        self.seen: list[tuple[str, dict]] = []
        self._pump_buffer()

    def _pump(self, limit: float) -> bool:
        self.sock.settimeout(min(limit, 0.5))
        try:
            d = self.sock.recv(65536)
        except socket.timeout:
            return True
        if not d:
            return False
        self.buf += d
        self._pump_buffer()
        return True

    def _pump_buffer(self) -> None:
        while b"\n\n" in self.buf:
            blk, self.buf = self.buf.split(b"\n\n", 1)
            lines = blk.decode().split("\n")
            if lines and lines[0].startswith("event: ") and len(lines) > 1 and lines[1].startswith("data: "):
                self.seen.append((lines[0][7:], json.loads(lines[1][6:])))

    def wait(self, name: str, pred=lambda d: True, limit: float = 5.0) -> dict:
        end = time.monotonic() + limit
        while True:
            for i, (n, d) in enumerate(self.seen):
                if n == name and pred(d):
                    del self.seen[i]  # consumed; other events stay for later waits
                    return d
            if time.monotonic() > end or not self._pump(end - time.monotonic()):
                raise AssertionError(f"no {name!r} event; saw {[n for n, _ in self.seen]}")


class DashboardBase(Base):
    def setUp(self) -> None:
        super().setUp()
        self.ck = self.login()

    def jcall(self, op, args=None):
        st, _, body = self.call(op, args, cookie=self.ck)
        return st, json.loads(body)


class TestPreflightShapes(DashboardBase):
    def test_default_preflight_unchanged(self):
        self.assertEqual(self.jcall("preflight")[1]["result"], {"ok": True})  # older tests rely on it

    def test_report_shape_for_every_mode(self):
        for mode in ("write", "read-only", "refuse"):
            self.fake.preflight_result = preflight_report(mode)
            st, out = self.jcall("preflight")
            self.assertEqual(st, 200)
            r = out["result"]
            self.assertEqual(r["mode"], mode)
            self.assertEqual(r["writes_allowed"], mode == "write")
            self.assertTrue(all(set(c) == {"name", "status", "reason"} and c["reason"] for c in r["checks"]), r)
            self.assertEqual({c["status"] for c in r["checks"]} - {"pass", "read-only", "refuse", "unknown", "missing"}, set())
        self.fake.preflight_result = preflight_report("write", unknown=True)
        r = self.jcall("preflight")[1]["result"]
        self.assertEqual((r["mode"], r["writes_allowed"]), ("write", False))  # UNKNOWN never allows writes (D-027 c)
        self.assertIn("unknown", [c["status"] for c in r["checks"]])


class TestHeartbeatBudget(DashboardBase):
    def test_heartbeat_carries_budget_fields_through_sse(self):
        sse = Sse(self, self.ck)
        self.fake.heartbeat("idle", ops_used=123)
        hb = sse.wait("heartbeat")
        self.assertEqual({k: hb[k] for k in ("state", "ops_used", "ops_budget")}, {"state": "idle", "ops_used": 123, "ops_budget": 600})
        self.fake.heartbeat("planning", ops_used=130, ops_budget=500, op_id="w7")
        hb = sse.wait("heartbeat", lambda d: d["state"] == "planning")
        self.assertEqual((hb["ops_used"], hb["ops_budget"], hb["op_id"]), (130, 500, "w7"))

    def test_new_stream_replays_latest_heartbeat_so_a_reload_sees_the_budget(self):
        self.fake.heartbeat("idle", ops_used=42)
        deadline = time.monotonic() + 3
        while "heartbeat" not in self.fake.link._latest and time.monotonic() < deadline:
            time.sleep(0.01)
        hb = Sse(self, self.ck).wait("heartbeat")
        self.assertEqual((hb["ops_used"], hb["ops_budget"]), (42, 600))


class TestTelemetryShape(DashboardBase):
    def test_telemetry_event_keeps_unavailable_leaves_as_null_values(self):
        sse = Sse(self, self.ck)
        st, out = self.jcall("telemetry.start", {"interval_s": 1})
        self.assertEqual((st, out["result"]["running"]), (200, True))
        self.fake.telemetry(telemetry_values(cores=3))
        ev = sse.wait("telemetry")
        v = ev["values"]
        self.assertEqual(sorted(v["cores"]), ["0", "1", "2"])  # JSON object keys are strings; the page sorts them numerically
        self.assertIsNone(v["cores"]["0"]["vid"]["value"])
        self.assertEqual(v["cores"]["0"]["vid"]["status"], "unavailable")
        self.assertTrue(v["cores"]["0"]["vid"]["reason"])
        self.assertEqual(v["cores"]["1"]["vid"]["status"], "ok")
        self.assertIsNone(v["vcore"]["value"])
        self.assertEqual(v["power"]["package"]["value"], 55.25)

    def test_sampler_error_snapshot_has_no_cores(self):
        sse = Sse(self, self.ck)
        self.fake.telemetry({"ts": 1.0, "status": "sampler error: OSError"})
        self.assertNotIn("cores", sse.wait("telemetry")["values"])


class TestVfDumpFlow(DashboardBase):
    def test_accepted_match_null_means_no_accepted_state(self):
        self.fake.vf_result = vf_dump_document(None)
        sse = Sse(self, self.ck)
        st, out = self.jcall("vf.dump", {})
        self.assertEqual(st, 202)
        res = sse.wait("result", lambda d: d["id"] == out["id"])
        self.assertTrue(res["ok"])
        self.assertIn("accepted_match", res["result"])
        self.assertIsNone(res["result"]["accepted_match"])
        self.assertEqual(res["result"]["ops_done"], 112)

    def test_accepted_match_list_shape(self):
        match = [{"cpu": 0, "match": True, "diffs": []},
                 {"cpu": 1, "match": False, "diffs": [{"point": 3, "expected": "0x0a2b3c4d", "actual": "0x0a2b3c40"}]}]
        self.fake.vf_result = vf_dump_document(match)
        sse = Sse(self, self.ck)
        _, out = self.jcall("vf.dump", {})
        res = sse.wait("result", lambda d: d["id"] == out["id"])["result"]
        self.assertEqual(res["accepted_match"], match)
        for m in res["accepted_match"]:
            self.assertEqual(set(m), {"cpu", "match", "diffs"})
            for d in m["diffs"]:
                self.assertEqual(set(d), {"point", "expected", "actual"})
        u = res["units"][0]["cpus"][0]
        self.assertEqual(set(u), {"cpu", "name", "oc", "points", "end"})
        self.assertEqual(set(u["points"][0]), {"point", "word", "ratio", "v_target_raw", "v_target_v", "static", "offset_raw", "offset_mv"})

    def test_older_helper_without_accepted_match_key(self):
        self.fake.vf_result = vf_dump_document()
        sse = Sse(self, self.ck)
        _, out = self.jcall("vf.dump", {})
        self.assertNotIn("accepted_match", sse.wait("result", lambda d: d["id"] == out["id"])["result"])

    def test_progress_events_and_result_for_one_op(self):
        self.fake.vf_result = vf_dump_document(None)
        self.fake.vf_progress = 3
        sse = Sse(self, self.ck)
        _, out = self.jcall("vf.dump", {})
        steps = []
        for _ in range(3):
            p = sse.wait("progress", lambda d: d["op_id"] == out["id"])
            steps.append((p["step"], p["of"], p["ops_budget"]))
        self.assertEqual(steps, [(1, 3, 600), (2, 3, 600), (3, 3, 600)])
        self.assertTrue(sse.wait("result", lambda d: d["id"] == out["id"])["ok"])

    def test_refusal_keeps_only_state_and_check_in_detail(self):
        self.fake.vf_error = {"code": "REFUSED", "message": "only 20 of 600 left", "detail": {"check": "BOOT_LIMIT", "needed": 112, "left": 20, "path": "/root/x"}}
        sse = Sse(self, self.ck)
        _, out = self.jcall("vf.dump", {})
        res = sse.wait("result", lambda d: d["id"] == out["id"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "REFUSED")
        self.assertEqual(res["error"]["detail"], {"check": "BOOT_LIMIT"})

    def test_cancel_ends_a_running_dump_with_cancelled(self):
        self.fake.cancel_cancels = True
        self.fake.hold.clear()
        sse = Sse(self, self.ck)
        st, out = self.jcall("vf.dump", {})
        self.assertEqual(st, 202)
        st, c = self.jcall("cancel", {"op_id": out["id"]})
        self.assertEqual((st, c["ok"], c["result"]["cancelled"]), (200, True, True))
        res = sse.wait("result", lambda d: d["id"] == out["id"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "CANCELLED")
        self.assertEqual(res["error"]["detail"], {"state": "planning"})  # the partial document never reaches the browser
        self.assertEqual([r["args"] for r in self.fake.requests if r["op"] == "cancel"], [{"op_id": out["id"]}])
        self.fake.hold.set()

    def test_default_cancel_is_still_busy_without_internal_detail(self):
        st, out = self.jcall("cancel", {"op_id": "w1"})
        self.assertEqual((st, out["error"]["code"], out["error"]["detail"]), (409, "BUSY", {"state": "applying"}))

    def test_result_is_replayed_to_a_reconnecting_tab_of_the_same_session(self):
        self.fake.vf_result = vf_dump_document(None)
        first = Sse(self, self.ck)
        _, out = self.jcall("vf.dump", {})
        first.wait("result", lambda d: d["id"] == out["id"])
        again = Sse(self, self.ck)  # reload: the page shows a result for an id it no longer remembers
        self.assertEqual(again.wait("result")["id"], out["id"])


class TestAuditTail(DashboardBase):
    def test_audit_tail_shape(self):
        self.fake.audit_records = [{"ts": "2026-10-08T10:00:00Z", "seq": i, "event": "attempt", "cpu": 0, "cmd": 17, "rc_name": "OK"} for i in range(1, 6)]
        st, out = self.jcall("audit.tail", {"n": 3})
        self.assertEqual(st, 200)
        r = out["result"]
        self.assertEqual([x["seq"] for x in r["records"]], [3, 4, 5])
        self.assertEqual(set(r), {"path", "records", "truncated"})
        self.assertFalse(r["truncated"])
