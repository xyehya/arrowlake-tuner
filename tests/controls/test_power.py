"""G-214: the power-limit control (``arltuner.controls.power``) -- decoding, the plan (bounds, order, lock), the read-back rule, the powercap files, the journal. No hardware."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arltuner.controls import power as pw
from arltuner.guard import load_policy
from arltuner.topology import arl_s_285k

from tests.fakesys import make_powercap

UW = 1_000_000
UNITS = pw.decode_units(0x000A0E03)  # 0.125 W, 2^-10 s [MEASURED lnx 2026-09-29]
UNIT_US = 976


def policy(power: str = "", extra: str = ""):
    return load_policy('cpus = "auto"\n' + power + extra, arl_s_285k())


FULL = "[power]\npl1_w = [100, 253]\npl2_w = [100, 295]\ntau_s = [1, 200]\nmax_step_w = 100\n"


def state(pl1=250, pl2=295, tau_us=127926272) -> pw.State:
    return pw.State(pl1 * UW, None if pl2 is None else pl2 * UW, tau_us, 2440, 125 * UW, {})


def view(sim: pw.SimPowerFiles | None = None, *, locked=False, units=UNITS) -> pw.MsrView:
    sim = sim or pw.SimPowerFiles(locked=locked)
    words = sim.msr_values()
    return pw.MsrView(units, pw.decode_limit(words[0x610], units), pw.decode_info(words[0x614], units))


class Decode(unittest.TestCase):
    def test_the_measured_0x610_of_2026_10_08(self) -> None:
        """``0x610`` = ``0x004287d000df87d0``: PL1 250 W, clamp, tau 56 s, PL2 250 W, lock 0 [MEASURED lnx 2026-10-08] (examples/reference-285k/notes/platform.md)."""
        lim = pw.decode_limit(0x004287D000DF87D0, UNITS)
        self.assertEqual((lim.pl1_w, lim.pl2_w, lim.locked), (250.0, 250.0, False))
        self.assertTrue(lim.pl1_enabled and lim.pl1_clamp and lim.pl2_enabled)
        self.assertAlmostEqual(lim.tau_s, 56.0, delta=0.1)

    def test_the_lock_bit(self) -> None:
        self.assertTrue(pw.decode_limit(0x004287D000DF87D0 | 1 << 63, UNITS).locked)

    def test_units_and_info(self) -> None:
        self.assertEqual((UNITS.power_w, UNITS.time_s), (0.125, 1 / 1024))
        info = pw.decode_info(0x3E8, UNITS)  # TDP 125 W [MEASURED] platform.md
        self.assertEqual((info.tdp_w, info.min_w, info.max_w), (125.0, 0.0, 0.0))

    def test_the_measured_window_is_2_17_times_976_us(self) -> None:
        """``constraint_0_time_window_us`` 127926272 [MEASURED lnx 2026-10-18] = 2^17 * 976 us: Y = 17, Z = 0 at the integer time unit 1000000 / 1024."""
        self.assertEqual(pw.tau_from_raw(17, UNIT_US), 127926272)
        self.assertEqual(pw.tau_to_raw(127926272, UNIT_US), 17)

    def test_tau_quantisation_brackets_the_request(self) -> None:
        self.assertEqual(pw.tau_quantized(127926272, UNIT_US), (127926272, 127926272))
        lo, hi = pw.tau_quantized(140_000_000, UNIT_US)
        self.assertEqual((lo, hi), (127926272, pw.tau_from_raw(17 | 1 << 5, UNIT_US)))
        self.assertLess(lo, 140_000_000)
        self.assertGreater(hi, 140_000_000)
        lo, hi = pw.tau_quantized(470_000_000, UNIT_US)  # Z = 3 rolls over into the next exponent
        self.assertEqual(hi, pw.tau_from_raw(19, UNIT_US))
        with self.assertRaises(pw.PowerError):
            pw.tau_quantized(500, UNIT_US)  # below one time unit


class PlanBounds(unittest.TestCase):
    def plan(self, intent, pol=None, st=None, v=None):
        return pw.plan(intent, pol or policy(FULL), st or state(), UNITS, v or view())

    def refused(self, intent, check, key=None, **kw):
        with self.assertRaises(pw.Refused) as ctx:
            self.plan(intent, **kw)
        self.assertEqual(ctx.exception.check, check, str(ctx.exception))
        if key is not None:
            self.assertEqual(ctx.exception.key, key)
        return str(ctx.exception)

    def test_a_value_inside_the_bounds_is_one_write(self) -> None:
        steps = self.plan({"pl1_w": 200})
        self.assertEqual([(s.key, s.file, s.before, s.value) for s in steps], [("pl1_w", "constraint_0_power_limit_uw", 250 * UW, 200 * UW)])

    def test_outside_the_range_is_refused_with_the_range(self) -> None:
        text = self.refused({"pl1_w": 99}, "POWER_RANGE", "pl1_w")
        self.assertIn("[100, 253]", text)
        self.refused({"pl1_w": 254}, "POWER_RANGE", "pl1_w")
        self.refused({"pl2_w": 296}, "POWER_RANGE", "pl2_w")
        self.refused({"tau_s": 0.5}, "POWER_RANGE", "tau_s")
        self.refused({"tau_s": 201}, "POWER_RANGE", "tau_s")

    def test_absent_policy_keys_refuse_and_name_the_key(self) -> None:
        for key, value in (("pl1_w", 200), ("pl2_w", 280), ("tau_s", 50)):
            text = self.refused({key: value}, "POWER_POLICY_MISSING", key, pol=policy())  # no [power] table at all
            self.assertIn(f"[power] {key}", text)
            self.assertIn("the policy", text)
        only_pl1 = policy("[power]\npl1_w = [100, 253]\nmax_step_w = 100\n")
        self.assertIn("[power] pl2_w", self.refused({"pl2_w": 280}, "POWER_POLICY_MISSING", "pl2_w", pol=only_pl1))
        self.assertEqual(len(self.plan({"pl1_w": 200}, pol=only_pl1)), 1)  # the key that is there still works

    def test_a_missing_step_limit_refuses_a_pl_change_and_names_it(self) -> None:
        no_step = policy("[power]\npl1_w = [100, 253]\n")
        text = self.refused({"pl1_w": 200}, "POWER_POLICY_MISSING", "max_step_w", pol=no_step)
        self.assertIn("[power] max_step_w", text)
        tau_only = policy("[power]\ntau_s = [1, 200]\n")  # tau needs no step limit
        self.assertEqual(len(self.plan({"tau_s": 100}, pol=tau_only)), 1)

    def test_the_step_limit(self) -> None:
        pol = policy("[power]\npl1_w = [50, 253]\npl2_w = [50, 295]\nmax_step_w = 40\n")
        self.refused({"pl1_w": 200}, "POWER_STEP", "pl1_w", pol=pol)  # 50 W down
        self.assertEqual(len(self.plan({"pl1_w": 210}, pol=pol)), 1)  # exactly 40 W is allowed

    def test_the_nothing_to_do_intent(self) -> None:
        self.refused({}, "NOTHING_TO_DO")
        self.assertEqual(self.plan({"pl1_w": 250}), [])  # already in force: no steps (the op says so)

    def test_values_that_the_register_cannot_hold(self) -> None:
        self.refused({"pl1_w": 200.5}, "POWER_VALUE", "pl1_w")
        self.refused({"pl1_w": True}, "POWER_VALUE", "pl1_w")
        self.refused({"tau_s": -1}, "POWER_VALUE", "tau_s")

    def test_pl2_must_not_be_below_pl1(self) -> None:
        self.refused({"pl2_w": 240}, "POWER_ORDER", "pl2_w")  # against the PL1 in force (250)
        self.refused({"pl1_w": 253}, "POWER_ORDER", "pl1_w", st=state(pl1=200, pl2=250))  # a single PL1 is checked against the PL2 in force
        self.refused({"pl1_w": 253, "pl2_w": 252}, "POWER_ORDER", "pl2_w")  # the pair as a whole
        self.assertEqual(len(self.plan({"pl1_w": 250, "pl2_w": 250})), 1)  # equal is allowed

    def test_no_pl2_file(self) -> None:
        self.refused({"pl1_w": 200}, "POWER_NO_FILE", "pl2_w", st=state(pl2=None))
        self.assertEqual(len(self.plan({"tau_s": 100}, st=state(pl2=None))), 1)


class PlanLock(unittest.TestCase):
    def test_the_lock_bit_makes_it_read_only(self) -> None:
        with self.assertRaises(pw.Refused) as ctx:
            pw.plan({"pl1_w": 200}, policy(FULL), state(), UNITS, view(locked=True))
        self.assertEqual(ctx.exception.check, "POWER_LOCKED")
        self.assertIn("bit 63", str(ctx.exception))

    def test_an_unreadable_0x610_refuses_writes(self) -> None:
        for v in (None, pw.MsrView(note="MSR 0x610: refused by the guard"), pw.MsrView(UNITS, None, None, "EIO")):
            with self.subTest(v=v), self.assertRaises(pw.Refused) as ctx:
                pw.plan({"pl1_w": 200}, policy(FULL), state(), UNITS, v)
            self.assertEqual(ctx.exception.check, "POWER_MSR_UNREADABLE")

    def test_the_policy_key_is_named_before_the_lock(self) -> None:
        with self.assertRaises(pw.Refused) as ctx:
            pw.plan({"pl1_w": 200}, policy(), state(), UNITS, view(locked=True))
        self.assertEqual(ctx.exception.check, "POWER_POLICY_MISSING")


class WriteOrder(unittest.TestCase):
    """The pair PL2 >= PL1 holds after every single write, whatever the direction of each."""

    POL = policy("[power]\npl1_w = [50, 300]\npl2_w = [50, 400]\ntau_s = [1, 500]\nmax_step_w = 400\n")

    def order(self, intent, **st):
        steps = pw.plan(intent, self.POL, state(**st), UNITS, view())
        pl1, pl2 = st.get("pl1", 250) * UW, st.get("pl2", 295) * UW
        for s in steps:
            if s.key == "pl1_w":
                pl1 = s.value
            elif s.key == "pl2_w":
                pl2 = s.value
            self.assertGreaterEqual(pl2, pl1, f"after step {s.number}: PL1 {pl1 / UW} PL2 {pl2 / UW}")
        return [s.key for s in steps]

    def test_lowering_both_writes_pl1_first(self) -> None:
        self.assertEqual(self.order({"pl1_w": 100, "pl2_w": 150}), ["pl1_w", "pl2_w"])

    def test_raising_both_writes_pl2_first(self) -> None:
        self.assertEqual(self.order({"pl1_w": 280, "pl2_w": 350}, pl1=100, pl2=150), ["pl2_w", "pl1_w"])

    def test_pl1_up_pl2_down(self) -> None:
        self.assertEqual(self.order({"pl1_w": 200, "pl2_w": 250}, pl1=100, pl2=300), ["pl1_w", "pl2_w"])  # 200 <= 300: PL1 first

    def test_pl1_down_pl2_up(self) -> None:
        self.assertEqual(self.order({"pl1_w": 100, "pl2_w": 350}, pl1=250, pl2=295), ["pl1_w", "pl2_w"])

    def test_pl1_up_past_the_old_pl2_needs_pl2_first(self) -> None:
        self.assertEqual(self.order({"pl1_w": 280, "pl2_w": 300}, pl1=100, pl2=150), ["pl2_w", "pl1_w"])

    def test_single_fields_and_tau_last(self) -> None:
        self.assertEqual(self.order({"pl2_w": 260}), ["pl2_w"])
        self.assertEqual(self.order({"tau_s": 30, "pl1_w": 200}), ["pl1_w", "tau_s"])


class ReadBack(unittest.TestCase):
    def step(self, key, value, before=None, expect=None):
        return pw.PowerStep(1, key, pw.FILES[key], before or 0, value, expect or (value, value))

    def test_a_clean_pl_write(self) -> None:
        sim = pw.SimPowerFiles()
        sim.write_int(pw.FILES["pl1_w"], 200 * UW)
        step = self.step("pl1_w", 200 * UW)
        self.assertIsNone(pw.readback_problem(step, sim.read_int(step.file), view(sim), UNITS))

    def test_a_silently_ignored_write_is_a_mismatch(self) -> None:
        sim = pw.SimPowerFiles(drop_writes=True)
        sim.write_int(pw.FILES["pl1_w"], 200 * UW)
        step = self.step("pl1_w", 200 * UW)
        self.assertIn("reads 250000000, expected 200000000", pw.readback_problem(step, sim.read_int(step.file), view(sim), UNITS))

    def test_the_register_disagreeing_with_the_file_is_a_mismatch(self) -> None:
        sim = pw.SimPowerFiles(msr_skew_w=10)
        sim.write_int(pw.FILES["pl1_w"], 200 * UW)
        step = self.step("pl1_w", 200 * UW)
        self.assertIn("MSR 0x610 decodes to 210 W", pw.readback_problem(step, sim.read_int(step.file), view(sim), UNITS))

    def test_the_lock_bit_after_the_write_is_a_mismatch(self) -> None:
        sim = pw.SimPowerFiles()
        sim.write_int(pw.FILES["pl2_w"], 280 * UW)
        sim.locked = True
        step = self.step("pl2_w", 280 * UW)
        self.assertIn("lock", pw.readback_problem(step, sim.read_int(step.file), view(sim), UNITS))

    def test_an_unreadable_register_or_file_is_not_a_confirmation(self) -> None:
        sim = pw.SimPowerFiles()
        step = self.step("pl1_w", 250 * UW)
        self.assertIn("cannot be read back", pw.readback_problem(step, None, view(sim), UNITS))
        self.assertIn("MSR 0x610 could not be read", pw.readback_problem(step, 250 * UW, pw.MsrView(note="x"), UNITS))

    def test_tau_accepts_either_bracketing_window_when_the_register_agrees(self) -> None:
        asked = 140_000_000
        lo, hi = pw.tau_quantized(asked, UNIT_US)
        step = self.step("tau_s", asked, expect=(lo, hi))
        sim = pw.SimPowerFiles()
        sim.write_int(pw.FILES["tau_s"], asked)  # the model rounds down
        self.assertEqual(sim.read_int(step.file), lo)
        self.assertIsNone(pw.readback_problem(step, lo, view(sim), UNITS))
        sim.tau_raw = pw.tau_to_raw(hi, UNIT_US)  # a kernel that rounds up
        self.assertIsNone(pw.readback_problem(step, hi, view(sim), UNITS))

    def test_tau_far_from_the_request_or_unchanged_is_a_mismatch(self) -> None:
        asked = 140_000_000
        step = self.step("tau_s", asked, expect=pw.tau_quantized(asked, UNIT_US))
        sim = pw.SimPowerFiles(drop_writes=True, tau_us=50_000_000)
        self.assertIn("expected", pw.readback_problem(step, sim.read_int(step.file), view(sim), UNITS))

    def test_tau_register_and_file_must_agree(self) -> None:
        asked = 127_926_272
        step = self.step("tau_s", asked)
        sim = pw.SimPowerFiles()
        sim.tau_raw = pw.tau_to_raw(asked // 2, UNIT_US)
        self.assertIn("decodes to", pw.readback_problem(step, asked, view(sim), UNITS))


class RealFiles(unittest.TestCase):
    """``PowerFiles`` over a fake sysfs tree (never the real /sys)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_the_measured_zone_reads(self) -> None:
        make_powercap(self.root)
        st = pw.read_state(pw.PowerFiles(self.root))
        self.assertEqual((st.pl1_uw, st.pl2_uw, st.tau_us, st.pl2_tau_us), (250 * UW, 295 * UW, 127926272, 2440))
        self.assertEqual((st.pl1_w, st.pl2_w, round(st.tau_s, 3)), (250.0, 295.0, 127.926))
        self.assertEqual(st.max_power_uw, 125 * UW)  # information only: 125 W is not a ceiling for the 250 W PL1

    def test_a_write_changes_one_file_and_reads_back(self) -> None:
        make_powercap(self.root)
        files = pw.PowerFiles(self.root)
        files.write_int(pw.FILES["pl2_w"], 275 * UW)
        zone = self.root / "intel-rapl:0"
        self.assertEqual((zone / "constraint_1_power_limit_uw").read_text().strip(), str(275 * UW))
        self.assertEqual((zone / "constraint_0_power_limit_uw").read_text().strip(), str(250 * UW))
        self.assertEqual(files.read_int(pw.FILES["pl2_w"]), 275 * UW)

    def test_no_zone_no_pl2_wrong_names(self) -> None:
        with self.assertRaises(pw.PowerError):
            pw.read_state(pw.PowerFiles(self.root))
        make_powercap(self.root, pl2_uw=None)
        self.assertIsNone(pw.read_state(pw.PowerFiles(self.root)).pl2_uw)
        make_powercap(self.root, names=("short_term", "long_term"))
        with self.assertRaises(pw.PowerError) as ctx:
            pw.read_state(pw.PowerFiles(self.root))
        self.assertIn("long_term", str(ctx.exception))

    def test_a_write_to_a_missing_file_is_a_power_error(self) -> None:
        make_powercap(self.root, pl2_uw=None)
        with self.assertRaises(pw.PowerError):
            pw.PowerFiles(self.root).write_int(pw.FILES["pl2_w"], 1)

    def test_a_symlink_leaf_is_not_followed(self) -> None:
        make_powercap(self.root)
        zone = self.root / "intel-rapl:0"
        target = self.root / "elsewhere"
        target.write_text("1\n")
        (zone / "constraint_1_power_limit_uw").unlink()
        (zone / "constraint_1_power_limit_uw").symlink_to(target)
        with self.assertRaises(pw.PowerError):
            pw.PowerFiles(self.root).write_int(pw.FILES["pl2_w"], 5)
        self.assertEqual(target.read_text(), "1\n")


class SimKernel(unittest.TestCase):
    def test_the_model_rounds_like_the_register(self) -> None:
        sim = pw.SimPowerFiles()
        sim.write_int(pw.FILES["pl1_w"], 200_050_000)  # 1/8 W granularity: rounded down
        self.assertEqual(sim.read_int(pw.FILES["pl1_w"]), 200_000_000)
        sim.write_int(pw.FILES["tau_s"], 140_000_000)
        self.assertEqual(sim.read_int(pw.FILES["tau_s"]), pw.tau_quantized(140_000_000, UNIT_US)[0])

    def test_a_locked_zone_ignores_the_write_and_the_register_says_so(self) -> None:
        sim = pw.SimPowerFiles(locked=True)
        sim.write_int(pw.FILES["pl1_w"], 100 * UW)
        self.assertEqual(sim.read_int(pw.FILES["pl1_w"]), 250 * UW)
        self.assertTrue(pw.decode_limit(sim.msr_values()[0x610], UNITS).locked)

    def test_the_registers_follow_the_files(self) -> None:
        sim = pw.SimPowerFiles()
        sim.write_int(pw.FILES["pl2_w"], 280 * UW)
        lim = pw.decode_limit(sim.msr_values()[0x610], UNITS)
        self.assertEqual((lim.pl1_w, lim.pl2_w, lim.tau_us), (250.0, 280.0, 127926272))


class Journal(unittest.TestCase):
    def test_round_trip_and_boot_binding(self) -> None:
        doc = pw.journal_doc(state(), "boot-a", 1.0, pid=7)
        data = json.dumps(doc).encode()
        self.assertEqual(pw.parse_journal(data, "boot-a"), {"pl1_uw": 250 * UW, "pl2_uw": 295 * UW, "tau_us": 127926272})
        self.assertIsNone(pw.parse_journal(data, "boot-b"))  # another boot: not applied
        self.assertIsNone(pw.parse_journal(data, None))

    def test_garbage_is_not_a_journal(self) -> None:
        for data in (b"not json", b"[]", json.dumps({"version": 2, "values": {}}).encode(), json.dumps({"version": 1, "boot_id": "b", "values": {"pl1_uw": -1}}).encode()):
            with self.subTest(data=data), self.assertRaises(ValueError):
                pw.parse_journal(data, "b")


class Revert(unittest.TestCase):
    BOOT = {"pl1_uw": 250 * UW, "pl2_uw": 295 * UW, "tau_us": 127926272}

    def test_revert_needs_no_policy_keys_and_no_step(self) -> None:
        steps = pw.plan_revert(self.BOOT, state(pl1=100, pl2=150, tau_us=50_000_000), UNITS, view())
        self.assertEqual([s.key for s in steps], ["pl2_w", "pl1_w", "tau_s"])  # raising: PL2 first, tau last
        self.assertEqual([s.value for s in steps], [295 * UW, 250 * UW, 127926272])

    def test_revert_of_what_is_already_in_force_is_empty(self) -> None:
        self.assertEqual(pw.plan_revert(self.BOOT, state(), UNITS, view()), [])

    def test_revert_still_stops_at_the_lock(self) -> None:
        with self.assertRaises(pw.Refused) as ctx:
            pw.plan_revert(self.BOOT, state(pl1=100), UNITS, view(locked=True))
        self.assertEqual(ctx.exception.check, "POWER_LOCKED")


if __name__ == "__main__":
    unittest.main()
