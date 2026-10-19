"""G-237: a late OC-point V target through the helper's ``tune.apply`` (simulator, ``late_target_reads``): the apply completes and the read-back rows carry ``polls`` / ``settle_ms`` so the page can say
"settled after N re-reads"; a row that matched on the first read carries neither."""
from __future__ import annotations

from arltuner.msr import SimConfig

from .test_tune import TuneCase, row, rows


class LateTargetApply(TuneCase):
    def test_the_rows_of_a_late_target_carry_the_polls(self) -> None:
        h = self.boot(SimConfig.measured(per_point=True, late_target_reads=2))
        self.read(h)
        res = self.apply_ok(h, self.plan(h, rows(row("EC1", ratio=48, v=1.1367))))
        self.assertEqual(self.word(12).to_u32(), 0x00048C30)
        self.assertTrue(all(rb["match"] for rb in res["readback"]), res["readback"])
        late = [rb for rb in res["readback"] if rb["point"] == 0 and rb.get("polls")]
        self.assertTrue(late, res["readback"])
        for rb in late:
            self.assertEqual(rb["polls"], 2)
            self.assertIsInstance(rb["settle_ms"], float)
        self.assertEqual(h.helper.tuned_this_boot, True)

    def test_without_the_lag_no_row_carries_polls(self) -> None:
        h = self.boot()
        self.read(h)
        res = self.apply_ok(h, self.plan(h, rows(row("EC1", ratio=48, v=1.1367))))
        self.assertTrue(all("polls" not in rb and "settle_ms" not in rb for rb in res["readback"]), res["readback"])
