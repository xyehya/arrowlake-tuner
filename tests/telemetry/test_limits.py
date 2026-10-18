import unittest

from arltuner.telemetry import limits


class Core64F(unittest.TestCase):
    def test_measured_word(self):
        # 0x64F read on every CPU of the 285K [MEASURED lnx 2026-10-13, captures/lnx/perf-limit-reasons-*.txt]
        r = limits.decode_core_64f(0x1DC21000)
        self.assertEqual(r.status, ("max_turbo_limit",))
        self.assertEqual(r.log, ("thermal", "vr_therm_alert", "vr_tdc", "other", "pl1", "pl2", "max_turbo_limit"))
        self.assertEqual(r.unknown_bits, ())
        self.assertEqual(r.raw, 0x1DC21000)

    def test_zero_is_empty(self):
        r = limits.decode_core_64f(0)
        self.assertEqual((r.status, r.log, r.unknown_bits), ((), (), ()))

    def test_every_documented_bit_and_its_log_bit(self):
        for bit, (key, _label) in limits.CORE_64F.items():
            self.assertEqual(limits.decode_core_64f(1 << bit).status, (key,))
            self.assertEqual(limits.decode_core_64f(1 << (bit + 16)).log, (key,))
            self.assertEqual(limits.decode_core_64f(1 << bit).log, ())

    def test_reserved_bits_reported_not_named(self):
        # bits 2, 3, 9, 14, 15 (and 18, 19, 25, 30, 31, >=32) are reserved in the SDM; bit 14 is NOT decoded (HWiNFO claim only)
        r = limits.decode_core_64f((1 << 14) | (1 << 2) | (1 << 25) | (1 << 40) | 1)
        self.assertEqual(r.status, ("prochot",))
        self.assertEqual(r.log, ())
        self.assertEqual(r.unknown_bits, (2, 14, 25, 40))

    def test_all_ones(self):
        r = limits.decode_core_64f((1 << 64) - 1)
        self.assertEqual(len(r.status), 11)
        self.assertEqual(len(r.log), 11)
        self.assertEqual(len(r.unknown_bits), 64 - 22)

    def test_to_json(self):
        self.assertEqual(limits.decode_core_64f(0x400).to_json(),
                         {"raw": 0x400, "status": ["pl1"], "log": [], "unknown_bits": []})

    def test_bad_words(self):
        for bad in (-1, 1 << 64, 1.0, None, True):
            with self.assertRaises(ValueError):
                limits.decode_core_64f(bad)


class Graphics6B0(unittest.TestCase):
    def test_documented_bits(self):
        r = limits.decode_graphics_6b0(0x10001000 | 1)
        self.assertEqual(r.status, ("prochot", "inefficient_operation"))
        self.assertEqual(r.log, ("inefficient_operation",))
        self.assertEqual(r.unknown_bits, ())

    def test_core_only_bits_are_unknown_here(self):
        # 4 (residency), 13 (turbo attenuation) exist in the 0x64F table only
        r = limits.decode_graphics_6b0((1 << 4) | (1 << 13) | (1 << 29))
        self.assertEqual((r.status, r.log), ((), ()))
        self.assertEqual(r.unknown_bits, (4, 13, 29))

    def test_every_documented_bit(self):
        for bit, (key, _label) in limits.GFX_6B0.items():
            self.assertEqual(limits.decode_graphics_6b0(1 << bit).status, (key,))
            self.assertEqual(limits.decode_graphics_6b0(1 << (bit + 16)).log, (key,))


if __name__ == "__main__":
    unittest.main()
