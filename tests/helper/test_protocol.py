"""Contract 8 framing and validation: JSON lines, 64 KiB cap, malformed input, unknown op / keys, ids, heartbeat. Pipes + the simulator only."""
from __future__ import annotations

import io
import json
import unittest

from arltuner.helper import protocol
from arltuner.helper.protocol import MAX_LINE, ProtocolError, Writer, parse_request, read_frame

from ._support import HelperCase, error_code


class FramingTests(unittest.TestCase):
    def test_line_at_the_cap_is_a_line_one_over_is_oversize(self) -> None:
        ok = b"x" * MAX_LINE + b"\n"
        over = b"x" * (MAX_LINE + 1) + b"\n"
        stream = io.BytesIO(ok + over + b'{"after":1}\n')
        self.assertEqual(read_frame(stream).kind, "line")
        self.assertEqual(read_frame(stream).kind, "oversize")
        after = read_frame(stream)  # the oversize line was drained: the next one is intact
        self.assertEqual((after.kind, after.data), ("line", b'{"after":1}'))
        self.assertEqual(read_frame(stream).kind, "eof")

    def test_huge_line_without_newline_is_drained_to_eof(self) -> None:
        stream = io.BytesIO(b"y" * (MAX_LINE * 5))
        self.assertEqual(read_frame(stream).kind, "oversize")
        self.assertEqual(read_frame(stream).kind, "eof")

    def test_last_line_without_newline_is_still_a_line(self) -> None:
        stream = io.BytesIO(b'{"id":"a","op":"preflight"}')
        frame = read_frame(stream)
        self.assertEqual((frame.kind, frame.data), ("line", b'{"id":"a","op":"preflight"}'))

    def test_parse_accepts_the_contract_shape(self) -> None:
        req = parse_request(b'{"id":"1","op":"audit.tail","args":{"n":3}}')
        self.assertEqual((req.id, req.op, req.args), ("1", "audit.tail", {"n": 3}))
        self.assertEqual(parse_request(b'{"id":"1","op":"preflight"}').args, {})  # args may be omitted

    def test_parse_rejects(self) -> None:
        bad = [
            b"", b"not json", b"[]", b'"x"', b"{}", b'{"id":"a"}', b'{"op":"x"}',
            b'{"id":1,"op":"x"}', b'{"id":"","op":"x"}', b'{"id":"' + b"a" * 65 + b'","op":"x"}',
            b'{"id":"a","op":""}', b'{"id":"a","op":5}', b'{"id":"a","op":"x","args":[]}', b'{"id":"a","op":"x","args":null}',
            b'{"id":"a","op":"x","extra":1}', b'{"id":"a","id":"b","op":"x"}', b'{"id":"a","op":"x","args":{"n":NaN}}',
            b"\xff\xfe", b"[" * 100000,
        ]
        for line in bad:
            with self.subTest(line=line[:40]), self.assertRaises(ProtocolError) as ctx:
                parse_request(line)
            self.assertEqual(ctx.exception.code, protocol.BAD_REQUEST)

    def test_id_is_kept_when_only_a_key_is_wrong(self) -> None:
        with self.assertRaises(ProtocolError) as ctx:
            parse_request(b'{"id":"keep","op":"x","extra":1}')
        self.assertEqual(ctx.exception.req_id, "keep")

    def test_writer_replaces_an_oversize_response_and_drops_an_oversize_event(self) -> None:
        buf = io.BytesIO()
        w = Writer(buf)
        self.assertTrue(w.send({"id": "r", "ok": True, "result": {"blob": "z" * (MAX_LINE + 10)}}))  # the replacement went out
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 1)  # the request still gets its one answer
        reply = json.loads(lines[0])
        self.assertEqual((reply["id"], reply["ok"], reply["error"]["code"]), ("r", False, "INTERNAL"))
        self.assertFalse(w.send({"event": "telemetry", "op_id": None, "values": "z" * (MAX_LINE + 10)}))
        self.assertEqual(len(buf.getvalue().splitlines()), 1)
        for line in buf.getvalue().splitlines():
            self.assertLessEqual(len(line), MAX_LINE)

    def test_writer_survives_a_broken_stream_and_reports_it_once(self) -> None:
        class Broken(io.RawIOBase):
            def writable(self) -> bool:
                return True

            def write(self, b):  # type: ignore[no-untyped-def]
                raise BrokenPipeError()

        calls: list[int] = []
        w = Writer(Broken(), on_broken=lambda: calls.append(1))
        self.assertFalse(w.send({"event": "x"}))
        self.assertFalse(w.send({"event": "x"}))
        self.assertEqual(calls, [1])


class WireTests(HelperCase):
    def test_every_request_gets_exactly_one_response_with_its_id(self) -> None:
        h = self.start()
        for i in range(3):
            self.assertTrue(h.call(f"p{i}", "preflight")["ok"])
        ids = [m["id"] for m in h.messages if "id" in m]
        self.assertEqual(ids, ["p0", "p1", "p2"])
        for m in h.messages:
            self.assertTrue(("event" in m) != ("id" in m), m)  # events never carry an id; responses always do

    def test_malformed_json_is_bad_request_with_null_id_and_the_helper_keeps_running(self) -> None:
        h = self.start()
        h.send_raw(b"{not json\n")
        reply = h.response(None)
        self.assertEqual(error_code(reply), "BAD_REQUEST")
        self.assertTrue(h.call("after", "preflight")["ok"])

    def test_oversize_line_is_bad_request_and_the_helper_keeps_running(self) -> None:
        h = self.start()
        h.send_raw(b'{"id":"big","op":"preflight","args":{"pad":"' + b"a" * (MAX_LINE + 100) + b'"}}\n')
        reply = h.response(None)
        self.assertEqual(error_code(reply), "BAD_REQUEST")
        self.assertIn("65536", reply["error"]["message"])
        self.assertTrue(h.call("after", "preflight")["ok"])

    def test_request_exactly_at_the_cap_is_parsed(self) -> None:
        h = self.start()
        pad = MAX_LINE - len(json.dumps({"id": "cap", "op": "preflight", "args": {"p": ""}}, separators=(",", ":")))
        line = json.dumps({"id": "cap", "op": "preflight", "args": {"p": "a" * pad}}, separators=(",", ":")).encode()
        self.assertEqual(len(line), MAX_LINE)
        h.send_raw(line + b"\n")
        reply = h.response("cap")
        self.assertEqual(error_code(reply), "BAD_REQUEST")
        self.assertIn("unknown arg", reply["error"]["message"])  # parsed, then refused for its arg (not for its size)

    def test_unknown_top_level_key_keeps_the_id(self) -> None:
        h = self.start()
        h.send({"id": "k", "op": "preflight", "extra": 1})
        reply = h.response("k")
        self.assertEqual(error_code(reply), "BAD_REQUEST")
        self.assertIn("extra", reply["error"]["message"])

    def test_unknown_args_are_bad_request(self) -> None:
        h = self.start()
        self.assertEqual(error_code(h.call("a1", "preflight", {"root": "/tmp"})), "BAD_REQUEST")  # preflight takes no input: the helper decides
        self.assertEqual(error_code(h.call("a2", "vf.dump", {"cpus": "0", "bogus": 1})), "BAD_REQUEST")
        self.assertEqual(error_code(h.call("a3", "vf.dump", {"cpus": "zero"})), "BAD_REQUEST")
        self.assertEqual(error_code(h.call("a4", "vf.dump", {"all_e": "yes"})), "BAD_REQUEST")
        self.assertEqual(error_code(h.call("a5", "audit.tail", {"n": 0})), "BAD_REQUEST")
        self.assertEqual(error_code(h.call("a6", "audit.tail", {"n": True})), "BAD_REQUEST")
        self.assertEqual(error_code(h.call("a7", "telemetry.start", {"interval_s": 0})), "BAD_REQUEST")
        self.assertEqual(error_code(h.call("a8", "cancel", {"op_id": 7})), "BAD_REQUEST")
        self.assertEqual(self.sim.rdmsr_count + self.sim.wrmsr_count, 0)  # nothing reached the device model

    def test_unknown_op_and_the_g2_g3_ops_are_unknown_op(self) -> None:
        h = self.start()
        for i, op in enumerate(["nope", "plan", "apply", "restore", "freq.cap", "freq.restore", "stress.start", "stress.stop", "Preflight", "vf.dump "]):
            with self.subTest(op=op):
                self.assertEqual(error_code(h.call(f"u{i}", op)), "UNKNOWN_OP")

    def test_reusing_a_finished_id_is_refused_with_a_null_id(self) -> None:
        h = self.start()
        self.assertTrue(h.call("same", "preflight")["ok"])
        h.send({"id": "same", "op": "preflight"})
        reply = h.wait_for(lambda m: m.get("id") is None and "error" in m)
        self.assertEqual(error_code(reply), "BAD_REQUEST")
        self.assertEqual(reply["error"]["detail"], {"id": "same"})
        self.assertEqual(len([m for m in h.messages if m.get("id") == "same"]), 1)  # the first answer is not duplicated

    def test_heartbeat_every_interval_with_the_state(self) -> None:
        h = self.start()
        h.wait_for(lambda m: m.get("event") == "heartbeat")
        beats = h.events("heartbeat")
        self.assertEqual({k for k in beats[0]}, {"event", "op_id", "state", "ops_used", "ops_budget"})
        self.assertEqual((beats[0]["op_id"], beats[0]["state"]), (None, "idle"))
        h.wait_for(lambda m: m.get("event") == "heartbeat", start=len(h.messages))  # and they keep coming

    def test_default_heartbeat_is_two_seconds(self) -> None:
        from arltuner.helper.config import HEARTBEAT_S

        self.assertEqual(HEARTBEAT_S, 2.0)

    def test_blank_line_is_bad_request(self) -> None:
        h = self.start()
        h.send_raw(b"\n")
        self.assertEqual(error_code(h.response(None)), "BAD_REQUEST")


if __name__ == "__main__":
    unittest.main()
