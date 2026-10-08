"""ts_driver.py carve/hash logic on a synthetic file (no real driver, no Windows calls)."""
import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ts_driver", ROOT / "tools" / "win" / "ts_driver.py")
ts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts)

H = lambda b: hashlib.sha256(b).hexdigest().upper()


class CarveTests(unittest.TestCase):
    def setUp(self):
        self.drv = bytes(range(256)) * 4
        self.exe = b"\xAA" * 100 + self.drv + b"\xBB" * 50
        self.kw = dict(exe_sha=H(self.exe), drv_sha=H(self.drv), offset=100, size=len(self.drv))

    def test_ok(self):
        self.assertEqual(ts.carve_bytes(self.exe, **self.kw), self.drv)

    def test_exe_hash_mismatch(self):
        with self.assertRaises(ts.DriverError):
            ts.carve_bytes(self.exe + b"x", **self.kw)

    def test_driver_hash_mismatch(self):
        with self.assertRaises(ts.DriverError):
            ts.carve_bytes(self.exe, **{**self.kw, "offset": 101})

    def test_too_short(self):
        with self.assertRaises(ts.DriverError):
            ts.carve_bytes(self.exe, **{**self.kw, "offset": 10_000})

    def test_carve_file_with_patched_constants(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "ts.exe"
            exe.write_bytes(self.exe)
            ts.EXE_SHA256, ts.DRIVER_SHA256 = self.kw["exe_sha"], self.kw["drv_sha"]
            ts.DRIVER_OFFSET, ts.DRIVER_SIZE = 100, len(self.drv)
            out = ts.carve(exe, Path(d) / "sub" / "x.sys")
            self.assertEqual(out.read_bytes(), self.drv)

    def test_constants_shape(self):
        self.assertEqual(len(ts.__dict__["EXE_SHA256"]), 64)


if __name__ == "__main__":
    unittest.main()
