"""G-128 (D-036): nothing in the helper is tied to the reference CPU. A simulated 6P+8E part (and 8P+12E) runs tune.read / tune.plan / tune.apply, the preflight
topology, ``read_at``, the favored cross-check and ``vf.measure`` through the real helper loop and the REAL advanced policy file, whose ``"auto"`` entries are
derived from the topology."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from arltuner.helper import ops_measure
from arltuner.msr import SimConfig, SimMsrPort
from arltuner.platform.preflight import preflight
from arltuner.topology import Topology, synthetic

from ._support import error_code
from .test_tune import TuneCase, row, rows

ISO_UTC = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
SIX_EIGHT = synthetic(6, 8, favored_p=(1, 4))
EIGHT_TWELVE = synthetic(8, 12, favored_p=(2,))


class LiveCase(TuneCase):
    def boot_topology(self, topo: Topology, **overrides: Any):
        config = SimConfig.measured(per_point=True, reject_zero_fields=True, topology=topo)
        self.sim = SimMsrPort(config)
        overrides.setdefault("topology_fn", lambda: topo)
        overrides.setdefault("preflight_fn", lambda: preflight(self.tmp / "nowhere", topology=topo))
        return self.boot(None, **overrides)


class SixPlusEight(LiveCase):
    def test_tune_read_decodes_the_units_of_the_part(self) -> None:
        h = self.boot_topology(SIX_EIGHT)
        res = self.read(h)
        self.assertEqual(sorted(res["tune"]), sorted([f"P{i}" for i in range(6)] + ["EC0", "EC1"]))
        self.assertEqual(sorted(h.helper.last_words), [*range(6), 6, 10])  # 6 P-cores + the lowest CPU of each E-cluster
        self.assertEqual([u["id"] for u in res["units"]], [f"P{i}" for i in range(6)] + ["EC0", "EC1"])
        self.assertEqual(sorted(res["fused"]), sorted(map(str, [*range(6), 6, 10])))
        p1 = res["tune"]["P1"]["cpus"][0]
        self.assertEqual([p["point"] for p in p1["points"]], list(range(9)))
        self.assertEqual(p1["points"][0]["point_label"], "OC (point 9)")  # 8 fused points: the OC point would be the 9th
        self.assertEqual(helper_fused_top(h, 1), 57)

    def test_read_at_is_iso_utc_and_tune_last_keeps_the_original_time(self) -> None:
        h = self.boot_topology(SIX_EIGHT, wall_clock=lambda: 1_790_000_000.0)
        res = self.read(h)
        self.assertRegex(res["read_at"], ISO_UTC)
        self.assertEqual(res["read_at"], "2026-09-21T14:13:20Z")
        last = h.call("l", "tune.last")["result"]
        self.assertTrue(last["available"])
        self.assertEqual(last["result"]["read_at"], res["read_at"])

    def test_tune_last_of_a_record_stored_before_read_at_gets_the_stored_time(self) -> None:
        h = self.boot_topology(SIX_EIGHT, wall_clock=lambda: 1_790_000_000.0)
        res = self.read(h)
        store = h.helper.cfg.audit_path.parent / "state"
        self.assertTrue(store.exists())
        from arltuner.helper import ops_tune

        record = ops_tune._boot_record(h.helper)
        del record["result"]["read_at"]
        ops_tune._last_store(h.helper).save(record)
        last = h.call("l", "tune.last")["result"]
        self.assertEqual(last["result"]["read_at"], res["read_at"])

    def test_the_favored_cross_check_is_quiet_when_sysfs_and_0x1c_agree(self) -> None:
        h = self.boot_topology(SIX_EIGHT)
        res = self.read(h)
        self.assertEqual(res["problems"], [])

    def test_the_favored_cross_check_notes_a_disagreement_and_does_not_fail(self) -> None:
        lying = synthetic(6, 8, favored_p=(0,))  # sysfs says P0 ...
        h = self.boot_topology(SIX_EIGHT, topology_fn=lambda: lying)  # ... the CPU's 0x1C says P1 and P4
        res = self.read(h)
        self.assertEqual(len(res["problems"]), 1)
        self.assertIn("favored cores differ", res["problems"][0])
        self.assertIn("sysfs", res["problems"][0])
        self.assertIn("P1, P4", res["problems"][0])
        self.assertTrue(res["complete"])

    def test_preflight_carries_the_topology_contract(self) -> None:
        h = self.boot_topology(SIX_EIGHT)
        topo = h.call("p", "preflight")["result"]["topology"]
        self.assertEqual(set(topo), {"units", "favored_source"})
        self.assertEqual(topo["favored_source"], "acpi_cppc")
        self.assertEqual([u["id"] for u in topo["units"]], [f"P{i}" for i in range(6)] + ["EC0", "EC1"])
        self.assertEqual([u["id"] for u in topo["units"] if u["favored"]], ["P1", "P4"])
        self.assertEqual(topo["units"][-1], {"id": "EC1", "kind": "E", "cpus": [10, 11, 12, 13], "favored": False, "max_ratio": 48, "perf": 68, "max_khz": 4_900_000, "ratio_model": "measured"})

    def test_plan_and_apply_work_on_this_part(self) -> None:
        h = self.boot_topology(SIX_EIGHT)
        self.read(h)
        p = self.plan(h, rows(row("P5", curve=-20), row("EC1", curve=-15)))
        self.assertEqual(p["units"], ["P5", "EC1"])
        self.apply_ok(h, p)
        self.assertEqual(self.word(5).offset_mv, VfWordMv(-20))
        self.assertEqual([round(self.word(c).offset_mv) for c in (10, 11, 12, 13)], [-15] * 4)  # the whole cluster of this part (CPUs 10-13)
        self.assertEqual(self.word(6).offset_raw, 0)  # the other cluster is untouched

    def test_a_unit_the_part_does_not_have_is_a_named_bad_request(self) -> None:
        h = self.boot_topology(SIX_EIGHT)
        self.read(h)
        for unit in ("P6", "EC2"):
            r = h.call(f"x{unit}", "tune.plan", {"intent": rows(row(unit, curve=-10))})
            self.assertEqual(error_code(r), "BAD_REQUEST", r)
            self.assertIn(unit, r["error"]["message"])
            self.assertIn("P0", r["error"]["message"])  # the message lists the units that exist

    def test_a_group_row_spreads_over_the_units_of_this_part_only(self) -> None:
        h = self.boot_topology(SIX_EIGHT)
        self.read(h)
        p = self.plan(h, {"scope": "simple", "rows": [row("E", curve=-10)]})
        self.assertEqual(p["units"], ["EC0", "EC1"])

    def test_the_cpu_limits_follow_the_part(self) -> None:
        h = self.boot_topology(SIX_EIGHT)
        self.read(h)
        p = self.plan(h, rows(row("EC1", ratio=50)))  # D-049: no policy cap (the old "auto" E maximum, 49) -- only the CPU's own limit counts
        self.assertTrue(p["ops_needed"] > 0)
        r = h.call("hi", "tune.plan", {"intent": rows(row("EC1", ratio=121))})
        self.assertEqual(error_code(r), "REFUSED")
        self.assertIn("CPU limit", r["error"]["message"])

    def test_measure_covers_exactly_the_units_of_the_part_and_says_which_plan_it_used(self) -> None:
        h = self.boot_topology(SIX_EIGHT)
        h.helper.measure_wiring = ops_measure.sim_wiring(self.tmp / "sim", time_scale=0.0, topology=SIX_EIGHT)
        generic = h.call("m1", "vf.measure", {"mode": "quick", "units": ["P1", "EC0"]}, timeout=120)["result"]
        self.assertEqual(generic["plan"], "generic")
        self.assertEqual(list(generic["record"]["units"]), ["P1", "EC0"])
        self.assertEqual(generic["record"]["units"]["EC0"]["cpus"], [6, 7, 8, 9])
        self.read(h)  # a kept read of this boot: the unit's own points
        kept = h.call("m2", "vf.measure", {"mode": "quick"}, timeout=240)["result"]
        self.assertEqual(kept["plan"], "kept read")
        self.assertEqual(list(kept["record"]["units"]), [f"P{i}" for i in range(6)] + ["EC0", "EC1"])
        self.assertEqual([p["ratio"] for p in kept["record"]["units"]["P1"]["points"]][:8], [8, 20, 30, 48, 53, 55, 56, 57])
        self.assertEqual(error_code(h.call("m3", "vf.measure", {"units": ["P6"]})), "BAD_REQUEST")


class EightPlusTwelve(LiveCase):
    def test_three_clusters(self) -> None:
        h = self.boot_topology(EIGHT_TWELVE)
        res = self.read(h)
        self.assertEqual(sorted(res["tune"]), sorted([f"P{i}" for i in range(8)] + ["EC0", "EC1", "EC2"]))
        p = self.plan(h, rows(row("EC2", curve=-10)))
        self.apply_ok(h, p)
        self.assertEqual([round(self.word(c).offset_mv) for c in (16, 17, 18, 19)], [-10] * 4)


class SimShape(LiveCase):
    def test_the_sim_helper_can_simulate_another_part(self) -> None:
        import argparse

        from arltuner.helper.main import _Usage, build_config, sim_topology

        self.assertEqual(sim_topology(argparse.Namespace(sim_shape=None)).unit_ids()[-1], "EC3")
        topo = sim_topology(argparse.Namespace(sim_shape="6+8"))
        self.assertEqual(topo.unit_ids(), ("P0", "P1", "P2", "P3", "P4", "P5", "EC0", "EC1"))
        for bad in ("6", "6+7", "0+8", "x+4"):
            with self.assertRaises(_Usage):
                sim_topology(argparse.Namespace(sim_shape=bad))
        args = argparse.Namespace(sim=True, device=False, policy=None, audit=str(self.audit), lock=str(self.lock), allow_taint_bit=[], state_dir=None, sim_shape="6+8")
        cfg = build_config(args, log=lambda t: None)
        self.assertEqual(cfg.topology_fn().unit_ids(), topo.unit_ids())
        self.assertEqual([u["id"] for u in cfg.preflight_fn().to_dict()["topology"]["units"]], list(topo.unit_ids()))
        self.assertEqual(sorted(c.cpu for c in cfg.port_factory().config.topology.cores), list(range(14)))


class NoFavoredCore(LiveCase):
    def test_all_equal_p_cores_have_no_favored_core(self) -> None:
        topo = synthetic(4, 4)
        h = self.boot_topology(topo)
        pre = h.call("p", "preflight")["result"]["topology"]
        self.assertIsNone(pre["favored_source"])
        self.assertFalse(any(u["favored"] for u in pre["units"]))


def VfWordMv(mv: int) -> float:
    """The offset field snaps to 1/1024 V: -20 mV is raw -20 = -19.53 mV."""
    from arltuner.mailbox import VfWord

    return VfWord(0, 0, False, VfWord.mv_to_offset_raw(mv)).offset_mv


def helper_fused_top(h: Any, cpu: int) -> int:
    return h.helper.fused_words[cpu] & 0xFF
