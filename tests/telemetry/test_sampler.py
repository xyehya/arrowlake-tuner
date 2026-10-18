import json
import tempfile
import time
import unittest
from pathlib import Path

from arltuner.telemetry import Sampler, TelemetryMsr
from arltuner.topology import arl_s_285k

from ._support import Clock, FakeMsrPort


class SamplerTests(unittest.TestCase):
    def test_no_msr_no_sysfs_everything_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            s = Sampler(None, hwmon_root=Path(td) / "h", powercap_root=Path(td) / "p", topology=arl_s_285k())
            snap = s.collect()
        json.dumps(snap)
        self.assertEqual(len(snap["cores"]), 24)
        for c in snap["cores"].values():
            for k in ("mhz", "busy", "vid", "ratio"):
                self.assertEqual(c[k]["status"], "unavailable")
                self.assertIsNone(c[k]["value"])
        self.assertEqual(snap["vcore"]["status"], "unavailable")
        self.assertEqual(snap["power"]["package"]["status"], "unavailable")
        self.assertEqual(snap["dram"]["read"]["status"], "unavailable")

    def test_with_fake_msr(self):
        clk = Clock()
        port = FakeMsrPort(default={0xCE: 0x2500, 0x198: (0x2000 << 32) | (0x30 << 8), 0xE7: 0, 0xE8: 0,
                                    0x1A2: 0x80691400, 0x19C: (1 << 31) | (20 << 16), 0x1B1: (1 << 31) | (25 << 16),
                                    0x606: 0xE0E03, 0x611: 0, 0x6B0: 0, 0x64F: 0x1DC21000})
        with tempfile.TemporaryDirectory() as td:
            s = Sampler(TelemetryMsr(port), hwmon_root=Path(td) / "h", powercap_root=Path(td) / "p", clock=clk, topology=arl_s_285k())
            s.collect()
            clk.t += 1
            port.default.update({0xE7: 3_700_000_000, 0xE8: 3_700_000_000, 0x611: 16384 * 50})
            snap = s.collect()
        c0 = snap["cores"][0]
        self.assertAlmostEqual(c0["mhz"]["value"], 3700.0)
        self.assertAlmostEqual(c0["busy"]["value"], 100.0)
        self.assertAlmostEqual(c0["vid"]["value"], 1.0)
        self.assertEqual(snap["power"]["package"]["value"], 50.0)  # MSR fallback (no powercap)
        self.assertEqual(snap["temps"]["Package"]["value"], 80)
        self.assertEqual(snap["temps"]["cpu3"]["value"], 85)
        self.assertEqual((snap["temps"]["cpu3"]["name"], snap["temps"]["cpu3"]["cpu"]), ("P-Core 3", 3))  # MSR fallback names
        self.assertEqual((snap["temps"]["Package"]["name"], snap["temps"]["Package"]["cpu"]), ("Package", None))
        self.assertEqual([m for _, m in port.reads if m == 0x150], [])

    def test_limit_reason_keys(self):
        clk = Clock()
        port = FakeMsrPort(default={0xCE: 0x2500, 0x198: 0, 0xE7: 0, 0xE8: 0, 0x1A2: 0x80691400,
                                    0x19C: (1 << 31), 0x1B1: (1 << 31), 0x606: 0xE0E03, 0x611: 0,
                                    0x64F: 0x1DC21000, 0x6B0: 0x10001000})
        with tempfile.TemporaryDirectory() as td:
            s = Sampler(TelemetryMsr(port), hwmon_root=Path(td) / "h", powercap_root=Path(td) / "p", clock=clk, topology=arl_s_285k())
            snap = s.collect()
        json.dumps(snap)
        core = snap["limit_reasons"]
        self.assertEqual(core["status"], "ok")
        self.assertEqual(core["value"], {"raw": 0x1DC21000, "status": ["max_turbo_limit"],
                                         "log": ["thermal", "vr_therm_alert", "vr_tdc", "other", "pl1", "pl2", "max_turbo_limit"],
                                         "unknown_bits": []})
        gfx = snap["limit_reasons_gfx"]
        self.assertEqual(gfx["status"], "ok")
        self.assertEqual(gfx["value"], {"raw": 0x10001000, "status": ["inefficient_operation"], "log": ["inefficient_operation"],
                                        "unknown_bits": []})
        self.assertEqual(snap["limit_reasons_raw"]["value"], 0x10001000)  # compat key: raw 0x6B0, unchanged
        self.assertEqual(snap["limit_reasons_raw"]["unit"], "raw")
        self.assertEqual([(c, m) for c, m in port.reads if m in (0x64F, 0x6B0)][:2], [(0, 0x64F), (0, 0x6B0)])  # CPU 0 only

    def test_limit_reasons_unavailable_cases(self):
        # 0x64F unreadable but 0x6B0 readable: one degraded value must not take the other down, and nothing is guessed
        port = FakeMsrPort(default={0xCE: 0x2500, 0x198: 0, 0xE7: 0, 0xE8: 0, 0x1A2: 0x80691400,
                                    0x19C: (1 << 31), 0x1B1: (1 << 31), 0x606: 0xE0E03, 0x611: 0, 0x6B0: 1})
        with tempfile.TemporaryDirectory() as td:
            snap = Sampler(TelemetryMsr(port), hwmon_root=Path(td) / "h", powercap_root=Path(td) / "p",
                           topology=arl_s_285k()).collect()
            no_msr = Sampler(None, hwmon_root=Path(td) / "h", powercap_root=Path(td) / "p", topology=arl_s_285k()).collect()
        self.assertEqual(snap["limit_reasons"]["status"], "unavailable")
        self.assertIsNone(snap["limit_reasons"]["value"])
        self.assertIn("0x64f", snap["limit_reasons"]["reason"])
        self.assertEqual(snap["limit_reasons_gfx"]["status"], "ok")
        self.assertEqual(snap["limit_reasons_gfx"]["value"]["status"], ["prochot"])
        self.assertEqual(snap["limit_reasons_gfx"]["value"]["raw"], 1)
        for key in ("limit_reasons", "limit_reasons_gfx", "limit_reasons_raw"):
            self.assertEqual(no_msr[key]["status"], "unavailable")
            self.assertIsNone(no_msr[key]["value"])
            self.assertEqual(no_msr[key]["reason"], "no MSR access (needs root)")

    def test_temp_names_from_core_id(self):
        # [MEASURED lnx 2026-10-11] CPU -> sysfs core_id; coretemp "Core N" labels carry the core_id
        core_id = {0: 0, 1: 4, 2: 16, 3: 20, 4: 24, 5: 28, 6: 40, 7: 44, **{c: c for c in range(8, 16)}, **{c: c + 16 for c in range(16, 24)}}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for cpu, cid in core_id.items():
                f = root / "cpu" / f"cpu{cpu}" / "topology" / "core_id"
                f.parent.mkdir(parents=True)
                f.write_text(f"{cid}\n")
            hw = root / "h" / "hwmon3"
            hw.mkdir(parents=True)
            (hw / "name").write_text("coretemp\n")
            labels = ["Package id 0"] + [f"Core {cid}" for cid in sorted(core_id.values())] + ["Core 99"]
            for i, lab in enumerate(labels, start=1):
                (hw / f"temp{i}_label").write_text(lab + "\n")
                (hw / f"temp{i}_input").write_text(f"{40000 + i * 100}\n")
            s = Sampler(None, hwmon_root=root / "h", powercap_root=root / "p", cpu_root=root / "cpu", topology=arl_s_285k())
            temps = s.collect()["temps"]
        self.assertEqual(len(temps), 26)
        self.assertEqual(temps["Core 4"]["name"], "P-Core 1")  # keys stay the raw labels; name/cpu are additive
        self.assertEqual(temps["Core 4"]["cpu"], 1)
        self.assertEqual(temps["Core 4"]["status"], "ok")
        self.assertEqual((temps["Core 44"]["name"], temps["Core 44"]["cpu"]), ("P-Core 7", 7))
        self.assertEqual((temps["Core 8"]["name"], temps["Core 8"]["cpu"]), ("E-Core 0", 8))
        self.assertEqual((temps["Core 15"]["name"], temps["Core 15"]["cpu"]), ("E-Core 7", 15))
        self.assertEqual((temps["Core 32"]["name"], temps["Core 32"]["cpu"]), ("E-Core 8", 16))
        self.assertEqual((temps["Core 39"]["name"], temps["Core 39"]["cpu"]), ("E-Core 15", 23))
        for cpu, cid in core_id.items():
            self.assertEqual(temps[f"Core {cid}"]["cpu"], cpu)
        self.assertEqual((temps["Package id 0"]["name"], temps["Package id 0"]["cpu"]), ("Package", None))
        self.assertEqual((temps["Core 99"]["name"], temps["Core 99"]["cpu"]), ("Core 99", None))  # unmapped stays raw
        json.dumps(temps)

    def test_temp_names_without_topology_files_stay_raw(self):
        with tempfile.TemporaryDirectory() as td:
            hw = Path(td) / "h" / "hwmon3"
            hw.mkdir(parents=True)
            (hw / "name").write_text("coretemp\n")
            (hw / "temp1_label").write_text("Core 4\n")
            (hw / "temp1_input").write_text("41000\n")
            t = Sampler(None, hwmon_root=Path(td) / "h", powercap_root=Path(td) / "p", cpu_root=Path(td) / "none", topology=arl_s_285k()).collect()["temps"]
        self.assertEqual((t["Core 4"]["name"], t["Core 4"]["cpu"]), ("Core 4", None))

    def test_thread_start_stop(self):
        with tempfile.TemporaryDirectory() as td:
            s = Sampler(None, hwmon_root=Path(td), powercap_root=Path(td), interval_s=0.01, topology=arl_s_285k())
            s.start()
            for _ in range(200):
                if s.snapshot().get("cores"):
                    break
                time.sleep(0.01)
            s.stop()
        self.assertTrue(s.snapshot().get("cores"))


if __name__ == "__main__":
    unittest.main()
