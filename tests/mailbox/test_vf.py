"""T-010 golden vectors for arltuner.mailbox.vf (source: docs/research/registers.md, "V/F data word")."""
import dataclasses
import random
import unittest

from arltuner.mailbox.vf import VfWord


class DecodeGolden(unittest.TestCase):
    def test_logged_xtu_word_ratio45_adaptive(self):
        self.assertEqual(VfWord.from_u32(0x0004A02D), VfWord(45, 0x4A0, False, 0))

    def test_trace_word_point4_48x_offset26(self):
        # registers.md "Third confirmation": 0x03400030 = 48x with offset raw 26 (+25.39 mV).
        # NOTE: the T-010 spec (and registers.md prose) also say the target is 0x400, but bits [19:8] of 0x03400030 are 0x000;
        # a 0x400 target would be the word 0x03440030. Both are asserted here so the discrepancy stays visible.
        w = VfWord.from_u32(0x03400030)
        self.assertEqual(w, VfWord(48, 0x000, False, 26))
        self.assertEqual(w.offset_mv, 25.390625)
        w_with_target = VfWord.from_u32(0x03440030)
        self.assertEqual(w_with_target, VfWord(48, 0x400, False, 26))
        self.assertEqual(w_with_target.offset_mv, 25.390625)
        self.assertEqual(w_with_target.v_target_v, 1.0)

    def test_trace_word_point8_57x(self):
        self.assertEqual(VfWord.from_u32(0x03400039).ratio, 57)
        self.assertEqual(VfWord.from_u32(0x03400039).offset_raw, 26)

    def test_v_target_volts(self):
        self.assertEqual(VfWord.from_u32(0x00048C2D).v_target_v, 1.13671875)
        self.assertEqual(VfWord.from_u32(0x0004A02D).v_target_v, 1.15625)

    def test_minus50mv_legacy_word(self):
        self.assertEqual(VfWord.from_u32(0xF9A00000).offset_raw, -51)
        self.assertEqual(VfWord(0, 0, False, -51).to_u32(), 0xF9A00000)

    def test_offset_field_is_0x7cd_for_minus50mv(self):
        # registers.md: "-50 mV = 0x7CD" (11-bit two's complement of -51)
        self.assertEqual((VfWord(0, 0, False, -51).to_u32() >> 21) & 0x7FF, 0x7CD)

    def test_offset_sign_extension_extremes(self):
        self.assertEqual(VfWord.from_u32(0x80000000).offset_raw, -1024)  # raw 0x400
        self.assertEqual(VfWord.from_u32(0x7FE00000).offset_raw, 1023)  # raw 0x3FF
        self.assertEqual(VfWord.from_u32(0xFFE00000).offset_raw, -1)

    def test_static_bit(self):
        self.assertTrue(VfWord.from_u32(0x00100000).static)
        self.assertFalse(VfWord.from_u32(0x000FFFFF).static)

    def test_from_u32_refuses_out_of_range(self):
        for bad in (-1, 2**32, 1.0, "0", None, True):
            with self.subTest(w=bad), self.assertRaises(ValueError):
                VfWord.from_u32(bad)


class Encode(unittest.TestCase):
    def test_static_bit_encodes_0x100000(self):
        self.assertEqual(VfWord(0, 0, True, 0).to_u32(), 0x100000)

    def test_logged_xtu_word(self):
        self.assertEqual(VfWord(45, 0x4A0, False, 0).to_u32(), 0x0004A02D)

    def test_field_isolation(self):
        self.assertEqual(VfWord(0xFF, 0, False, 0).to_u32(), 0xFF)
        self.assertEqual(VfWord(0, 0xFFF, False, 0).to_u32(), 0xFFF00)
        self.assertEqual(VfWord(0, 0, False, -1).to_u32(), 0xFFE00000)
        self.assertEqual(VfWord(0, 0, False, 1023).to_u32(), 0x7FE00000)
        self.assertEqual(VfWord(0, 0, False, -1024).to_u32(), 0x80000000)


class MvToOffsetRaw(unittest.TestCase):
    def test_xtu_throttlestop_rounding(self):
        self.assertEqual(VfWord.mv_to_offset_raw(-50), -51)
        self.assertEqual(VfWord.mv_to_offset_raw(25), 26)

    def test_range_ends(self):
        self.assertEqual(VfWord.mv_to_offset_raw(-1000), -1024)
        self.assertEqual(VfWord.mv_to_offset_raw(999), 1023)

    def test_out_of_range_refused(self):
        for bad in (1000, -1001, 5000, -5000):
            with self.subTest(mv=bad), self.assertRaises(ValueError):
                VfWord.mv_to_offset_raw(bad)

    def test_zero_and_sign_symmetry(self):
        self.assertEqual(VfWord.mv_to_offset_raw(0), 0)
        self.assertEqual(VfWord.mv_to_offset_raw(-0.0), 0)
        for mv in (1, 10, 49.5, 123, 500, 998):
            with self.subTest(mv=mv):
                self.assertEqual(VfWord.mv_to_offset_raw(-mv), -VfWord.mv_to_offset_raw(mv))

    def test_matches_integer_formula_for_every_whole_mv(self):
        for mv in range(-1000, 1000):
            expected = (abs(mv) * 1024 + 512) // 1000
            self.assertEqual(VfWord.mv_to_offset_raw(mv), -expected if mv < 0 else expected, mv)

    def test_float_input_uses_exact_arithmetic(self):
        self.assertEqual(VfWord.mv_to_offset_raw(-50.0), -51)
        self.assertEqual(VfWord.mv_to_offset_raw(25.0), 26)
        # 0.4 mV -> (409.6+512)//1000 = 0 ; 0.5 mV -> (512+512)//1000 = 1
        self.assertEqual(VfWord.mv_to_offset_raw(0.4), 0)
        self.assertEqual(VfWord.mv_to_offset_raw(0.5), 1)

    def test_non_numbers_and_non_finite_refused(self):
        for bad in ("10", None, True, float("nan"), float("inf"), float("-inf"), [1]):
            with self.subTest(mv=bad), self.assertRaises(ValueError):
                VfWord.mv_to_offset_raw(bad)


class VToRaw(unittest.TestCase):
    def test_xtu_ui_voltages(self):
        self.assertEqual(VfWord.v_to_raw(1.137), 0x48C)
        self.assertEqual(VfWord.v_to_raw(1.426), 0x5B4)
        self.assertEqual(VfWord.v_to_raw(1.316), 0x544)

    def test_exact_grid_points(self):
        self.assertEqual(VfWord.v_to_raw(1.0), 0x400)
        self.assertEqual(VfWord.v_to_raw(0.0), 0)
        self.assertEqual(VfWord.v_to_raw(1.15625), 0x4A0)

    def test_non_numbers_and_non_finite_refused(self):
        for bad in ("1.0", None, True, float("nan"), float("inf")):
            with self.subTest(volts=bad), self.assertRaises(ValueError):
                VfWord.v_to_raw(bad)


class Validation(unittest.TestCase):
    def test_field_range_violations_raise(self):
        bad_fields = [
            dict(ratio=-1), dict(ratio=256),
            dict(v_target_raw=-1), dict(v_target_raw=4096),
            dict(offset_raw=-1025), dict(offset_raw=1024),
        ]
        for kw in bad_fields:
            with self.subTest(**kw), self.assertRaises(ValueError):
                VfWord(**{"ratio": 0, "v_target_raw": 0, "static": False, "offset_raw": 0, **kw})

    def test_boundary_values_accepted(self):
        VfWord(0, 0, False, -1024)
        VfWord(255, 4095, True, 1023)

    def test_non_int_fields_refused(self):
        for kw in (dict(ratio=45.0), dict(v_target_raw="1"), dict(offset_raw=1.5), dict(ratio=True), dict(offset_raw=None)):
            with self.subTest(**kw), self.assertRaises(ValueError):
                VfWord(**{"ratio": 0, "v_target_raw": 0, "static": False, "offset_raw": 0, **kw})

    def test_static_must_be_bool(self):
        for bad in (0, 1, None, "False"):
            with self.subTest(static=bad), self.assertRaises(ValueError):
                VfWord(0, 0, bad, 0)  # type: ignore[arg-type]

    def test_frozen(self):
        w = VfWord(45, 0x4A0, False, 0)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            w.ratio = 46  # type: ignore[misc]

    def test_with_replaces_and_validates(self):
        w = VfWord(45, 0x4A0, False, 0)
        self.assertEqual(w.with_(offset_raw=-51), VfWord(45, 0x4A0, False, -51))
        self.assertEqual(w.with_(ratio=57, static=True), VfWord(57, 0x4A0, True, 0))
        self.assertEqual(w, VfWord(45, 0x4A0, False, 0))  # original untouched
        self.assertEqual(w.with_(), w)
        with self.assertRaises(ValueError):
            w.with_(ratio=256)
        with self.assertRaises(ValueError):
            w.with_(offset_raw=1024)
        with self.assertRaises(ValueError):
            w.with_(v_target_raw=-1)
        with self.assertRaises(ValueError):
            w.with_(static=1)
        with self.assertRaises(TypeError):
            w.with_(bogus=1)

    def test_offset_mv_property(self):
        self.assertEqual(VfWord(0, 0, False, 26).offset_mv, 25.390625)
        self.assertEqual(VfWord(0, 0, False, -1024).offset_mv, -1000.0)
        self.assertEqual(VfWord(0, 0, False, 0).offset_mv, 0.0)


class RoundTrips(unittest.TestCase):
    def test_words_roundtrip(self):
        rng = random.Random(0x285_0001)
        for _ in range(3000):
            w = VfWord(rng.randrange(256), rng.randrange(4096), rng.random() < 0.5, rng.randrange(-1024, 1024))
            self.assertEqual(VfWord.from_u32(w.to_u32()), w)

    def test_u32_roundtrip(self):
        rng = random.Random(0x285_0002)
        for x in [0, 1, 0xFFFFFFFF, 0x80000000, 0x7FFFFFFF, 0x00100000] + [rng.randrange(2**32) for _ in range(3000)]:
            self.assertEqual(VfWord.from_u32(x).to_u32(), x)

    def test_to_u32_always_fits_32_bits(self):
        rng = random.Random(0x285_0003)
        for _ in range(1000):
            w = VfWord(rng.randrange(256), rng.randrange(4096), rng.random() < 0.5, rng.randrange(-1024, 1024))
            self.assertTrue(0 <= w.to_u32() <= 0xFFFFFFFF)

    def test_mv_roundtrip_through_offset_field(self):
        for mv in range(-1000, 1000, 7):
            raw = VfWord.mv_to_offset_raw(mv)
            decoded = VfWord.from_u32(VfWord(0, 0, False, raw).to_u32())
            self.assertEqual(decoded.offset_raw, raw)
            # (|mv|*1024+512)//1000 rounds with a 0.512 raw-LSB bias, so the decode error is at most 0.512 LSB = 0.5 mV
            self.assertLessEqual(abs(decoded.offset_mv - mv), 0.5 + 1e-9)


if __name__ == "__main__":
    unittest.main()
