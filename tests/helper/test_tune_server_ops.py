"""G-112: the web server's ``/api/call`` allowlist knows the contract-9 ops of this task with the right long/short flag and an argument filter that mirrors
the helper's (outer filter only: the helper's validators are the authority), and every op that is long in the helper is long in the server."""
from __future__ import annotations

import unittest

from arltuner.helper.ops import builtin_ops
from arltuner.web.server import OPS

PLAN_ID = "a" * 64
INTENT = {"scope": "unit", "rows": [{"target": "P0", "offset": {"kind": "curve", "mv": -10}, "v_target_v": None, "ratio": None}]}


class ServerOps(unittest.TestCase):
    def test_long_flags_agree_with_the_helper(self) -> None:
        helper_ops = builtin_ops()
        for name in ("tune.read", "tune.plan", "tune.apply", "profiles.list", "profiles.save", "vf.dump"):
            self.assertIn(name, OPS, name)
            self.assertIn(name, helper_ops, name)
            self.assertEqual(OPS[name][1], helper_ops[name].long, name)

    def test_tune_read_takes_the_vf_dump_arguments(self) -> None:
        validate, _ = OPS["tune.read"]
        self.assertTrue(validate({}))
        self.assertTrue(validate({"cpus": [0, 1], "all_e": False}))
        self.assertFalse(validate({"cpus": "0-3"}))
        self.assertFalse(validate({"x": 1}))

    def test_tune_plan(self) -> None:
        validate, _ = OPS["tune.plan"]
        self.assertTrue(validate({"intent": INTENT}))
        self.assertTrue(validate({"intent": {"reset": "stock"}}))
        for bad in ({}, {"intent": []}, {"intent": INTENT, "x": 1}, {"intent": "reset"}, {"intent": {"pad": "x" * 20000}}):
            self.assertFalse(validate(bad), bad)

    def test_tune_apply(self) -> None:
        validate, _ = OPS["tune.apply"]
        self.assertTrue(validate({"plan_id": PLAN_ID, "typed": "CONFIRM ABC234"}))
        for bad in ({}, {"plan_id": PLAN_ID}, {"plan_id": "xyz", "typed": "CONFIRM ABC234"}, {"plan_id": PLAN_ID, "typed": "CONFIRM abc234"},
                    {"plan_id": PLAN_ID, "typed": "CONFIRM ABC231"}, {"plan_id": PLAN_ID, "typed": "CONFIRM ABC234", "x": 1}):
            self.assertFalse(validate(bad), bad)

    def test_profiles(self) -> None:
        self.assertTrue(OPS["profiles.list"][0]({}))
        self.assertFalse(OPS["profiles.list"][0]({"x": 1}))
        save = OPS["profiles.save"][0]
        self.assertTrue(save({"name": "my-p3.v1", "intent": INTENT}))
        for bad in ({}, {"name": "My", "intent": INTENT}, {"name": "ok"}, {"name": "../x", "intent": INTENT}, {"name": "ok", "intent": []}, {"name": "x" * 65, "intent": INTENT}):
            self.assertFalse(save(bad), bad)


if __name__ == "__main__":
    unittest.main()
