"""G-229: throttle indicators -- window duty, debounce, event log, tail, kernel counters, the sampler end to end. Fakes only; nothing is written to hardware."""
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from arltuner.msr.port import MsrError
from arltuner.telemetry import Sampler, TelemetryMsr, msrio, throttle
from arltuner.telemetry.throttle import EventLog, KernelThermal, ScriptedWords, ThrottleTracker
from arltuner.topology import arl_s_285k

from ._support import Clock, FakeMsrPort

PL1, PL2, EDP, VRTDC, THERMAL, PROCHOT, RATL, VRTHERM, MAXTURBO = 1 << 10, 1 << 11, 1 << 8, 1 << 7, 1 << 1, 1 << 0, 1 << 5, 1 << 6, 1 << 12


class Rig:
    """A tracker on a manual clock; ``window(words, **ctx)`` feeds one window and drains it (1 s long)."""

    def __init__(self, td: str, **kw) -> None:
        self.clk, self.wall = Clock(100.0), Clock(1_700_000_000.0)
        self.log = EventLog(Path(td) / "throttle.jsonl")
        self.tr = ThrottleTracker(self.clk, self.wall, self.log, **kw)

    def window(self, words, **ctx):
        for w in words:
            self.tr.add(w)
        self.clk.t += 1
        self.wall.t += 1
        return self.tr.drain(ctx).value

    def records(self):
        p = self.log.path
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


class WindowTests(unittest.TestCase):
    def test_duty_and_bits_per_group(self):
        with tempfile.TemporaryDirectory() as td:
            r = Rig(td)
            v = r.window([PL1, PL1 | EDP, 0, 0, 0, PL2, 0, 0, 0, 0])
        g = v["groups"]
        self.assertEqual([g[k]["label"] for k in g], ["Power limit", "Current / EDP", "Thermal", "VR thermal", "Max turbo cap"])
        self.assertEqual((g["power"]["active"], g["power"]["duty"], g["power"]["bits"]), (True, 0.3, ["pl1", "pl2"]))
        self.assertEqual((g["current"]["active"], g["current"]["duty"], g["current"]["bits"]), (True, 0.1, ["other"]))
        self.assertEqual((g["thermal"]["active"], g["thermal"]["duty"], g["thermal"]["bits"]), (False, 0.0, []))
        self.assertEqual(v["samples"], 10)
        self.assertEqual(v["bit_duty"], {"pl1": 0.2, "pl2": 0.1, "other": 0.1})

    def test_each_group_takes_its_bits(self):
        with tempfile.TemporaryDirectory() as td:
            r = Rig(td)
            for word, group in ((VRTDC, "current"), (PROCHOT, "thermal"), (RATL, "thermal"), (THERMAL, "thermal"), (VRTHERM, "vr_thermal"), (MAXTURBO, "max_turbo")):
                g = r.window([word])["groups"]
                self.assertEqual([k for k, x in g.items() if x["active"]], [group], hex(word))

    def test_log_bits_never_light_an_indicator_and_unknown_bits_are_numbers(self):
        with tempfile.TemporaryDirectory() as td:
            r = Rig(td)
            v = r.window([0x1DC20000 | (1 << 14) | (1 << 13) | (1 << 4)])  # log bits 31:16 set; status: 14 (unknown), 13 and 4 (info only)
        self.assertEqual([k for k, x in v["groups"].items() if x["active"]], [])
        self.assertEqual(v["unknown_bits"], [14])
        self.assertEqual(v["info_bits"], ["residency_state_regulation", "turbo_transition_attenuation"])

    def test_no_samples_is_unavailable_with_the_reason(self):
        with tempfile.TemporaryDirectory() as td:
            r = Rig(td)
            r.tr.add_failure("fake: msr 0x64f unimplemented")
            rd = r.tr.drain()
        self.assertIsNone(rd.value)
        self.assertIn("unimplemented", rd.reason)


class EventTests(unittest.TestCase):
    def test_opens_on_first_window_closes_after_two_clean_ones(self):
        with tempfile.TemporaryDirectory() as td:
            r = Rig(td, tuning_id_fn=lambda: "accepted-2026-10-15")
            r.window([0] * 10)
            r.window([PL1] * 5 + [0] * 5, temp_c=70, pkg_w=200, mhz=5000)
            r.window([PL2] * 10, temp_c=85, pkg_w=250, mhz=5700, kernel_thermal_delta=2)
            self.assertEqual(r.records(), [])  # still open
            r.window([0] * 10, temp_c=60)
            self.assertEqual(r.records(), [])  # one clean window is not enough
            r.window([0] * 10)
            (ev,) = r.records()
        self.assertEqual((ev["group"], ev["label"], ev["bits"]), ("power", "Power limit", ["pl1", "pl2"]))
        self.assertEqual(ev["dur_ms"], 2000)  # first active window start .. last active window end
        self.assertEqual(ev["t_off"] - ev["t_on"], 2.0)
        self.assertAlmostEqual(ev["duty"], 0.75)
        self.assertEqual((ev["peak_temp_c"], ev["peak_pkg_w"], ev["peak_mhz"], ev["kernel_thermal_delta"]), (85, 250, 5700, 2))
        self.assertEqual(ev["tuning_id"], "accepted-2026-10-15")

    def test_a_single_clean_window_between_active_ones_is_one_event(self):
        with tempfile.TemporaryDirectory() as td:
            r = Rig(td)
            for words in ([EDP], [0], [VRTDC], [0], [0]):
                r.window(words)
            (ev,) = r.records()
        self.assertEqual((ev["group"], ev["bits"], ev["dur_ms"]), ("current", ["vr_tdc", "other"], 3000))

    def test_groups_are_independent_and_max_turbo_is_never_logged(self):
        with tempfile.TemporaryDirectory() as td:
            r = Rig(td)
            r.window([MAXTURBO | THERMAL | PL1])
            r.window([MAXTURBO | THERMAL])
            for _ in range(2):
                r.window([MAXTURBO])
            self.assertEqual(sorted(e["group"] for e in r.records()), ["power", "thermal"])
            r.window([0])
            r.window([0])
            self.assertNotIn("max_turbo", [e["group"] for e in r.records()])

    def test_flush_writes_open_events(self):
        with tempfile.TemporaryDirectory() as td:
            r = Rig(td)
            r.window([PL1])
            r.tr.flush()
            self.assertEqual([e["group"] for e in r.records()], ["power"])

    def test_tuning_id_failure_does_not_break_sampling(self):
        def boom():
            raise RuntimeError("x")

        with tempfile.TemporaryDirectory() as td:
            r = Rig(td, tuning_id_fn=boom)
            r.window([PL1])
            r.tr.flush()
            self.assertIsNone(r.records()[0]["tuning_id"])


class LogTests(unittest.TestCase):
    def test_file_is_0600_and_append_only(self):
        with tempfile.TemporaryDirectory() as td:
            log = EventLog(Path(td) / "sub" / "throttle.jsonl")
            self.assertTrue(log.append({"a": 1}))
            self.assertTrue(log.append({"a": 2}))
            self.assertEqual(stat.S_IMODE(os.stat(log.path).st_mode), 0o600)
            self.assertEqual(len(log.path.read_text().splitlines()), 2)

    def test_rotation_keeps_one_generation(self):
        with tempfile.TemporaryDirectory() as td:
            log = EventLog(Path(td) / "throttle.jsonl", max_bytes=100)
            for i in range(12):
                log.append({"i": i, "pad": "x" * 20})
            self.assertTrue(log.path.with_name("throttle.jsonl.1").exists())
            self.assertLessEqual(os.stat(log.path).st_size, 100 + 60)
            ids = [r["i"] for r in throttle.tail(log.path, 200)["records"]]
            self.assertEqual(ids, sorted(ids))
            self.assertEqual(ids[-1], 11)

    def test_symlink_is_refused_and_the_error_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "victim"
            target.write_text("keep")
            link = Path(td) / "throttle.jsonl"
            link.symlink_to(target)
            log = EventLog(link)
            self.assertFalse(log.append({"a": 1}))
            self.assertIsNotNone(log.error)
            self.assertEqual(target.read_text(), "keep")

    def test_tail_bounds_and_validation(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "throttle.jsonl"
            self.assertEqual(throttle.tail(p, 5), {"path": str(p), "records": [], "truncated": False})
            log = EventLog(p)
            for i in range(30):
                log.append({"i": i})
            res = throttle.tail(p, 5)
            self.assertEqual([r["i"] for r in res["records"]], [25, 26, 27, 28, 29])
            self.assertTrue(res["truncated"])
            with p.open("ab") as f:
                f.write(b"not json\n")
            self.assertIn("unparsable", throttle.tail(p, 1)["records"][0])
            big = Path(td) / "big.jsonl"
            big.write_bytes((json.dumps({"pad": "y" * 900}) + "\n").encode() * 1000)
            os.chmod(big, 0o600)
            res = throttle.tail(big, 200)
            self.assertLessEqual(len(json.dumps(res["records"])), 60 * 1024)
            self.assertTrue(res["truncated"])
        self.assertEqual(throttle.validate_tail_n({}), 20)
        self.assertEqual(throttle.validate_tail_n({"n": 200}), 200)
        for bad in ({"n": 0}, {"n": 201}, {"n": True}, {"n": "5"}, {"n": 1.5}, {"x": 1}):
            with self.assertRaises(ValueError):
                throttle.validate_tail_n(bad)

    def test_log_sits_beside_the_audit_log(self):
        self.assertEqual(throttle.throttle_log_path(Path("/var/lib/arltuner/audit.jsonl")), Path("/var/lib/arltuner/throttle.jsonl"))


class KernelThermalTests(unittest.TestCase):
    def test_diff_per_tick_and_package_counted_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for cpu in (0, 1):
                d = root / f"cpu{cpu}" / "thermal_throttle"
                d.mkdir(parents=True)
                (d / "core_throttle_count").write_text("0\n")
                (d / "package_throttle_count").write_text("0\n")
            kt = KernelThermal([0, 1, 2], root)  # cpu2 has no directory
            first = kt.sample().value
            self.assertEqual((first["core_delta"], first["package_delta"]), (0, 0))
            (root / "cpu0" / "thermal_throttle" / "core_throttle_count").write_text("3\n")
            for cpu in (0, 1):
                (root / f"cpu{cpu}" / "thermal_throttle" / "package_throttle_count").write_text("2\n")
            second = kt.sample().value
            self.assertEqual((second["core_delta"], second["package_delta"], second["package_count"]), (3, 2, 2))

    def test_missing_sysfs_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            r = KernelThermal([0], Path(td)).sample()
        self.assertFalse(r.available)


class ChangingPort(FakeMsrPort):
    """0x64F returns the next word of a script on each read (None = the read fails)."""

    def __init__(self, words, **kw):
        super().__init__(**kw)
        self.words, self.i = list(words), 0

    def rdmsr(self, cpu, msr):
        if msr == 0x64F:
            self.reads.append((cpu, msr))
            w = self.words[min(self.i, len(self.words) - 1)]
            self.i += 1
            if w is None:
                raise MsrError(5, "fake EIO")
            return w
        return super().rdmsr(cpu, msr)


BASE = {0xCE: 0x2500, 0x198: 0, 0xE7: 0, 0xE8: 0, 0x1A2: 0x80691400, 0x19C: 1 << 31, 0x1B1: (1 << 31) | (1 << 10), 0x606: 0xE0E03, 0x611: 0, 0x6B0: 0x01000000, 0x6B1: 0x400}


class SamplerThrottleTests(unittest.TestCase):
    def sampler(self, td, port, **kw):
        return Sampler(TelemetryMsr(port), hwmon_root=Path(td) / "h", powercap_root=Path(td) / "p", cpu_root=Path(td) / "c", clock=Clock(), topology=arl_s_285k(), **kw)

    def test_oversampled_window_lands_in_the_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            port = ChangingPort([PL1, 0, 0, PL2, 0, None, 0, 0, 0, 0, 0], default=BASE)
            s = self.sampler(td, port, throttle_log=Path(td) / "throttle.jsonl", tuning_id_fn=lambda: "t1")
            for _ in range(10):  # ten between-tick reads (one of them fails); collect() adds the eleventh word
                s._oversample()
            snap = s.collect()
            json.dumps(snap)
            thr = snap["throttle"]
            self.assertEqual(thr["status"], "ok")
            v = thr["value"]
            self.assertEqual((v["samples"], v["failed"]), (10, 1))
            self.assertEqual(v["groups"]["power"]["duty"], 0.2)
            self.assertEqual(snap["limit_reasons"]["value"]["raw"], 0)
            self.assertEqual(snap["limit_reasons_ring"]["value"]["status"], ["pl1"])
            self.assertEqual(snap["pkg_therm_status"]["value"]["status"], ["power_limit"])
            self.assertEqual(snap["limit_reasons_raw"]["value"], 0x01000000)  # compat key kept for the old page
            self.assertEqual(snap["kernel_thermal"]["status"], "unavailable")
            s.stop()
            self.assertEqual([e["group"] for e in throttle.tail(Path(td) / "throttle.jsonl")["records"]], ["power"])
        self.assertEqual({m for _, m in port.reads} - msrio.TELEMETRY_MSRS, set())
        self.assertTrue(all(cpu == 0 for cpu, m in port.reads if m in (0x64F, 0x6B1)))  # 0x64F / 0x6B1 on CPU 0 only

    def test_the_loop_oversamples_between_ticks(self):
        with tempfile.TemporaryDirectory() as td:
            port = FakeMsrPort(default={**BASE, 0x64F: 0})
            s = self.sampler(td, port, interval_s=0.35, oversample_hz=20.0)
            s.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and s.snapshot().get("ts") is None:
                time.sleep(0.02)
            time.sleep(0.5)
            s.stop()
            snap = s.snapshot()
        self.assertEqual(snap["throttle"]["status"], "ok")
        self.assertGreaterEqual(len([1 for _, m in port.reads if m == 0x64F]), 5)

    def test_no_msr_no_throttle_but_a_reason(self):
        with tempfile.TemporaryDirectory() as td:
            s = Sampler(None, hwmon_root=Path(td) / "h", powercap_root=Path(td) / "p", cpu_root=Path(td) / "c", topology=arl_s_285k())
            snap = s.collect()
        self.assertEqual(snap["throttle"]["status"], "unavailable")
        self.assertIn("no MSR access", snap["throttle"]["reason"])
        self.assertEqual(snap["limit_reasons_ring"]["status"], "unavailable")

    def test_scripted_words_are_the_simulator_knob(self):
        clk = Clock(0.0)
        words = ScriptedWords.parse("0x400:3, 0:5", clk)
        seen = []
        for t in (0.0, 3.5, 8.0):
            clk.t = t
            seen.append(words())
        self.assertEqual(seen, [0x400, 0, 0x400])
        with self.assertRaises(ValueError):
            ScriptedWords.parse("0x400:0")
        with tempfile.TemporaryDirectory() as td:
            s = Sampler(None, hwmon_root=Path(td) / "h", powercap_root=Path(td) / "p", cpu_root=Path(td) / "c", topology=arl_s_285k(), limit_words=ScriptedWords([(PL1, 5)], clk))
            snap = s.collect()
        self.assertTrue(snap["throttle"]["value"]["groups"]["power"]["active"])
        self.assertIn("simulated", snap["limit_reasons"]["source"])


class AllowlistTests(unittest.TestCase):
    def test_6b1_is_a_telemetry_read_and_nothing_else(self):
        from arltuner.guard import policy

        self.assertIn(0x6B1, msrio.TELEMETRY_MSRS)
        for msr in (0x6B1, 0x64F, 0x6B0, 0x19C, 0x1B1):
            self.assertNotIn(msr, policy.PLAIN_MSR_READABLE)
        self.assertEqual(TelemetryMsr(FakeMsrPort(default={0x6B1: 7})).rdmsr(0, 0x6B1), 7)
        self.assertFalse(hasattr(TelemetryMsr(FakeMsrPort()), "wrmsr"))

    def test_no_msr_write_or_device_open_in_the_throttle_code(self):
        root = Path(throttle.__file__).parent
        for name in ("throttle.py", "sampler.py", "cpu.py", "limits.py"):
            text = (root / name).read_text(encoding="utf-8")
            for banned in ("wrmsr(", ".write_msr", "/dev/cpu"):
                self.assertNotIn(banned, text, f"{name} mentions {banned}")


if __name__ == "__main__":
    unittest.main()
