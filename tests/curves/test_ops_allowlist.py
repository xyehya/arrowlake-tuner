"""The web server's ``/api/call`` allowlist for the G-111 ops (contract 9) agrees with the helper's own validators."""
from __future__ import annotations

import unittest

from arltuner.helper.ops import builtin_ops
from arltuner.helper.protocol import OpError
from arltuner.web.server import OPS, public_response

CID = "20261011T120000Z-ab12"
CASES = [
    ("vf.measure", {}, True), ("vf.measure", {"mode": "quick"}, True), ("vf.measure", {"mode": "dense", "units": ["P0", "EC3"], "name": "x"}, True),
    ("vf.measure", {"mode": "fast"}, False), ("vf.measure", {"units": []}, False), ("vf.measure", {"units": ["P0", "P0"]}, False), ("vf.measure", {"units": ["Q8"]}, False), ("vf.measure", {"units": ["P8"]}, True),
    ("vf.measure", {"name": ""}, False), ("vf.measure", {"name": "x" * 65}, False), ("vf.measure", {"extra": 1}, False), ("vf.measure", {"units": "P0"}, False),
    ("curves.list", {}, True), ("curves.list", {"x": 1}, False),
    ("curves.get", {"id": CID}, True), ("curves.get", {"id": CID, "units": ["P1"]}, True), ("curves.get", {}, False), ("curves.get", {"id": "../x"}, False),
    ("curves.get", {"id": CID, "units": []}, False),
    ("curves.update", {"id": CID, "name": "n"}, True), ("curves.update", {"id": CID, "baseline": True}, True), ("curves.update", {"id": CID}, False),
    ("curves.update", {"id": CID, "baseline": 1}, False), ("curves.update", {"id": CID, "name": ""}, False),
    ("curves.delete", {"id": CID}, True), ("curves.delete", {}, False), ("curves.delete", {"id": CID, "x": 1}, False),
    ("mem.read", {}, True), ("mem.read", {"x": 1}, False),
]


class AllowlistTests(unittest.TestCase):
    def test_server_and_helper_agree(self) -> None:
        ops = builtin_ops()
        for op, args, ok in CASES:
            validate, _long = OPS[op]
            self.assertEqual(validate(args), ok, f"server {op} {args}")
            try:
                ops[op].validate(args)
                helper_ok = True
            except OpError:
                helper_ok = False
            self.assertEqual(helper_ok, ok, f"helper {op} {args}")

    def test_only_vf_measure_is_long_and_it_is_not_write_gated(self) -> None:
        for op in ("vf.measure", "curves.list", "curves.get", "curves.update", "curves.delete", "mem.read"):
            self.assertEqual(OPS[op][1], op == "vf.measure", op)
            self.assertEqual(builtin_ops()[op].long, op == "vf.measure", op)
            self.assertFalse(builtin_ops()[op].write, op)

    def test_the_curve_id_of_a_cancelled_run_reaches_the_browser(self) -> None:
        out = public_response({"id": "m", "ok": False, "error": {"code": "CANCELLED", "message": "c", "detail": {"curve_id": CID, "partial": {"big": 1}}}})
        self.assertEqual(out["error"]["detail"], {"curve_id": CID})


if __name__ == "__main__":
    unittest.main()
