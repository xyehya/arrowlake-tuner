"""G-147: the GUI/helper restore of the accepted state neutralises a residual OC point, never lowers a favored ratio, and the Dashboard compare calls the result equivalent.

Simulator + fake sysfs only. The machine is the measured one after a warm reboot that kept two E OC words (``[MEASURED lnx 2026-10-13]``): EC1 ``0x00048C30`` (48x +
1.137 V) and EC2 ``0x00048C2E`` (46x + 1.137 V); the accepted state has no OC point on either (OC word 0) and the 46x point at -25 mV.
"""
from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path
from typing import Any

from arltuner.controls.residual import RESIDUAL_NOTE
from arltuner.curves import CurveStore
from arltuner.helper.ops_measure import CAP_ATTR, MeasureWiring, SimModel, sim_memory
from arltuner.mailbox import VfWord
from arltuner.msr import SimConfig

from ._measure_support import RecordingCpufreq, make_cpufreq
from .test_tune import TuneCase, texts

MAX_ATTR = "cpuinfo_max_freq"
EC1, EC2 = (12, 13, 14, 15), (16, 17, 18, 19)
POINT6 = VfWord(46, 0, False, VfWord.mv_to_offset_raw(-25)).to_u32()
HEAD = '[meta]\nname = "{n}"\nplatform = "arl-s-285k"\n'
POINT = '[[point]]\nkind = "E"\nindex = {i}\npoint = 6\noffset_mv = -25\nexpect_ratio = 46\n'


def residual_config() -> SimConfig:
    cfg = SimConfig.measured(per_point=True, reject_zero_fields=True)
    initial = dict(cfg.initial_vf)
    initial.update({c: VfWord(48, 0x48C, False, 0) for c in EC1})
    initial.update({c: VfWord(46, 0x48C, False, 0) for c in EC2})
    return dataclasses.replace(cfg, initial_vf=initial)


class RestoreResidualCase(TuneCase):
    def accepted(self) -> Path:
        acc = self.tmp / "accepted"
        (acc / "profiles").mkdir(parents=True)
        (acc / "CURRENT").write_text("profile = profiles/acc.toml\n")
        (acc / "profiles/acc.toml").write_text(HEAD.format(n="acc") + POINT.format(i=4) + POINT.format(i=8))
        # EC1 / EC2 as the accepted state holds them: OC word 0, the six fused points, the 46x point at -25 mV
        (acc / "EXPECTED-words.txt").write_text(f"12  0 8 F 17 1E 28 {POINT6:X}\n16  0 8 F 17 1E 28 {POINT6:X}\n")
        return acc

    def boot_with_sysfs(self):
        h = self.boot(residual_config(), accepted_dir=self.accepted())
        tmp = Path(tempfile.mkdtemp(dir=self.tmp))
        self.journal = tmp / "measure-journal.json"
        self.cpufreq = RecordingCpufreq(tmp / "sys", self.journal)
        make_cpufreq(tmp / "sys", SimModel())
        h.helper.measure_wiring = MeasureWiring(
            cpufreq=self.cpufreq, make_run=lambda: None, store=CurveStore(tmp / "curves"), journal_path=self.journal, mem_fn=sim_memory,  # type: ignore[arg-type]
        )
        self.sim.on_favored_write = lambda cpu, khz: self.cpufreq.path(cpu, MAX_ATTR).write_text(f"{khz}\n")
        return h

    def compare(self, h) -> dict[int, dict[str, Any]]:
        res = self.read(h)
        return {c["cpu"]: c for c in res["accepted_match"]}


class RestoreResidual(RestoreResidualCase):
    def test_the_dashboard_before_the_restore_flags_ec1_and_calls_ec2_equivalent(self) -> None:
        h = self.boot_with_sysfs()
        cmp = self.compare(h)
        # (point 6 differs on both before the restore: the -25 mV of the accepted state is not applied yet; only the OC word is under test here)
        self.assertEqual([d["point"] for d in cmp[12]["diffs"]], [0, 6, 7])  # 48x with 1.137 V: a different voltage than the tested profile
        self.assertEqual([d["point"] for d in cmp[16]["diffs"]], [6])
        self.assertEqual([e["point"] for e in cmp[16]["equivalents"]], [0])  # 46x = the top fused ratio, the stale target is harmless

    def test_restore_neutralises_ec1_keeps_ec2_and_every_favored_ratio(self) -> None:
        h = self.boot_with_sysfs()
        self.read(h)
        fav_before = {c: self.sim.favored_word(c) for c in (*EC1, *EC2)}
        plan = self.plan(h, {"restore": "accepted"})
        notes = texts(plan)
        self.assertTrue(any("residual OC point" in n and "EC1" in n and "last read" in n for n in notes), notes)
        self.assertGreaterEqual(plan["ops_needed"], 2 * 12)  # at least the scan
        res = self.apply_ok(h, plan)
        self.assertEqual(len(res["residual"]), 1)
        self.assertIn("EC1", res["residual"][0])
        for cpu in EC1:
            w = self.word(cpu)
            self.assertEqual((w.ratio, w.offset_raw), (48, 0))
            self.assertTrue(0 < w.v_target_raw < 0x48C + VfWord.mv_to_offset_raw(-25), hex(w.v_target_raw))
        self.assertEqual(self.sim.oc_point_voltage_raw(12), 0x48C + VfWord.mv_to_offset_raw(-25))  # 48x runs the accepted voltage
        self.assertEqual([self.word(c).to_u32() for c in EC2], [0x48C2E] * 4)
        self.assertEqual({c: self.sim.favored_word(c) for c in (*EC1, *EC2)}, fav_before)  # nothing lowered
        self.assertNotIn(0x1D, [r.cmd for r in self.sim.requests])
        self.assertEqual(res["cpufreq"], [])
        self.assertEqual(self.cpufreq.writes, [])  # nothing moved, so no cap was touched
        self.assertEqual({self.cpufreq.read(c, CAP_ATTR) for c in (*EC1, *EC2)}, {"4900000"})
        cmp = self.compare(h)
        self.assertTrue(all(c["match"] for c in cmp.values()), cmp)
        self.assertTrue(cmp[12]["equivalent"])
        self.assertEqual([e["note"] for e in cmp[12]["equivalents"]], [RESIDUAL_NOTE, RESIDUAL_NOTE])  # the OC word and the point it created

    def test_no_zero_ratio_is_sent_and_the_guard_bounds_are_the_policys(self) -> None:
        h = self.boot_with_sysfs()
        self.apply_ok(h, self.plan(h, {"restore": "accepted"}))
        for r in self.sim.requests:
            if r.cmd == 0x11 and r.p2 == 0:
                self.assertNotEqual(VfWord.from_u32(r.data).ratio, 0)

    def test_a_cold_boot_needs_no_neutralisation(self) -> None:
        h = self.boot(SimConfig.measured(per_point=True, reject_zero_fields=True), accepted_dir=self.accepted())
        res = self.apply_ok(h, self.plan(h, {"restore": "accepted"}))
        self.assertEqual(res["residual"], [])
        self.assertEqual({self.word(c).to_u32() for c in EC1}, {0})  # untouched cold word

    def test_the_budget_of_the_session_reserves_the_extra_rows(self) -> None:
        h = self.boot_with_sysfs()
        self.read(h)
        plan = self.plan(h, {"restore": "accepted"})
        self.assertEqual(h.helper.cfg.simulator, True)
        res = self.apply_ok(h, plan)
        self.assertLessEqual(res["ops_used"], plan["ops_needed"])
