import tempfile
import unittest
from pathlib import Path

from arltuner.telemetry.hwmon import read_coretemp, read_vcore
from arltuner.telemetry.rapl import PowercapPower

from ._support import Clock


def w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="ascii")


class Hwmon(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def test_coretemp(self):
        w(self.root / "hwmon3/name", "coretemp\n")
        w(self.root / "hwmon3/temp1_label", "Package id 0\n")
        w(self.root / "hwmon3/temp1_input", "45500\n")
        w(self.root / "hwmon3/temp2_label", "Core 0\n")
        w(self.root / "hwmon3/temp2_input", "garbage\n")
        w(self.root / "hwmon3/temp3_label", "Core 1\n")  # input missing
        w(self.root / "hwmon1/name", "nvme\n")
        r = read_coretemp(self.root)
        self.assertEqual(r["Package id 0"].value, 45.5)
        self.assertIsNone(r["Core 0"].value)
        self.assertIsNone(r["Core 1"].value)
        self.assertEqual(read_coretemp(self.root / "nope"), {})

    def test_vcore(self):
        chip, label = "nct6686", "VIN16"
        self.assertIsNone(read_vcore(self.root, chip, label).value)  # empty root
        w(self.root / "hwmon5/name", "nct6686\n")
        w(self.root / "hwmon5/in1_label", "VIN0\n")
        w(self.root / "hwmon5/in1_input", "1000\n")
        r = read_vcore(self.root, chip, label)
        self.assertIsNone(r.value)  # VIN0 is present but is not Vcore -> unavailable, never another rail
        self.assertIn("VIN16", r.reason)
        w(self.root / "hwmon5/in0_label", "VIN16\n")
        w(self.root / "hwmon5/in0_input", "1456\n")
        r = read_vcore(self.root, chip, label)
        self.assertAlmostEqual(r.value, 1.456)
        self.assertEqual(r.unit, "V")
        self.assertIsNone(read_vcore(self.root, "nct6799", label).value)  # a different chip name does not match
        self.assertIsNone(read_vcore(self.root, chip, "Vcore").value)  # nor a different label
        self.assertIsNone(read_vcore(self.root, "", label).value)  # not recorded
        self.assertIsNone(read_vcore(self.root, chip, "").value)
        w(self.root / "hwmon6/name", "nct6799\n")
        w(self.root / "hwmon6/in0_label", "VIN16\n")
        w(self.root / "hwmon6/in0_input", "999\n")
        self.assertAlmostEqual(read_vcore(self.root, chip, label).value, 1.456)  # only the named chip is read


class Powercap(unittest.TestCase):
    def test_power_wrap_and_missing(self):
        with tempfile.TemporaryDirectory() as td:
            z = Path(td) / "intel-rapl:0"
            w(z / "name", "package-0\n")
            w(z / "max_energy_range_uj", "1000000\n")
            w(z / "energy_uj", "900000\n")
            clk = Clock()
            p = PowercapPower("intel-rapl:0", Path(td), clk)
            self.assertFalse(p.sample().available)
            clk.t += 2.0
            w(z / "energy_uj", "100000\n")  # wrapped: 200001 uJ in 2 s
            self.assertAlmostEqual(p.sample().value, 0.1000005, places=6)
            self.assertEqual(p.name(), "package-0")
            (z / "energy_uj").unlink()
            clk.t += 1
            r = p.sample()
            self.assertIsNone(r.value)
            self.assertIn("energy_uj", r.reason)
            self.assertIsNone(PowercapPower("intel-rapl:9", Path(td), clk).sample().value)


if __name__ == "__main__":
    unittest.main()
