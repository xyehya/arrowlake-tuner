"""T-010 golden vectors for arltuner.mailbox.words (source: docs/research/registers.md, "OC mailbox -- request model")."""
import ast
import inspect
import random
import unittest

from arltuner.mailbox import commands, vf, words
from arltuner.mailbox.words import MSR_OC_MAILBOX, Response, is_busy, pack, unpack


class PackGolden(unittest.TestCase):
    def test_msr_constant(self):
        self.assertEqual(MSR_OC_MAILBOX, 0x150)

    def test_vf_write_ratio45_vtarget(self):
        self.assertEqual(pack(0x11, 0, 0, 0x0004A02D), 0x800000110004A02D)

    def test_vf_write_minus50mv_legacy_word(self):
        self.assertEqual(pack(0x11, 0, 0, 0xF9A00000), 0x80000011F9A00000)

    def test_favored_core_write(self):
        self.assertEqual(pack(0x1D, 0, 0, 0x39), 0x8000001D00000039)

    def test_p1_lands_in_bits_47_40(self):
        self.assertEqual(pack(0x10, 2, 0, 0), 0x8000021000000000)

    def test_p2_lands_in_bits_55_48(self):
        self.assertEqual(pack(0x10, 0, 4, 0), 0x8004001000000000)

    def test_busy_bit_always_set(self):
        self.assertTrue(pack(0, 0, 0, 0) >> 63 & 1)
        self.assertEqual(pack(0, 0, 0, 0), 0x8000000000000000)

    def test_maximum_fields(self):
        self.assertEqual(pack(0xFF, 0xFF, 0xFF, 0xFFFFFFFF), 0x80FFFFFFFFFFFFFF)  # bits 62..56 are not part of the request

    def test_accepts_int_enum_members(self):
        self.assertEqual(pack(commands.Cmd.VOLTAGE_FREQ_WRITE, commands.Domain.CORE, 0, 0x0004A02D), 0x800000110004A02D)


class PackRefusals(unittest.TestCase):
    def test_cmd_out_of_range(self):
        for bad in (-1, 256, 2**32):
            with self.subTest(cmd=bad), self.assertRaises(ValueError):
                pack(bad, 0, 0, 0)

    def test_p1_out_of_range(self):
        for bad in (-1, 256):
            with self.subTest(p1=bad), self.assertRaises(ValueError):
                pack(0x10, bad, 0, 0)

    def test_p2_out_of_range(self):
        for bad in (-1, 256):
            with self.subTest(p2=bad), self.assertRaises(ValueError):
                pack(0x10, 0, bad, 0)

    def test_data_out_of_range(self):
        for bad in (-1, 2**32, 2**64):
            with self.subTest(data=bad), self.assertRaises(ValueError):
                pack(0x10, 0, 0, bad)

    def test_non_int_rejected_in_every_field(self):
        for bad in (1.0, "1", None, b"\x01", True, False, [1]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    pack(bad, 0, 0, 0)
                with self.assertRaises(ValueError):
                    pack(0x10, bad, 0, 0)
                with self.assertRaises(ValueError):
                    pack(0x10, 0, bad, 0)
                with self.assertRaises(ValueError):
                    pack(0x10, 0, 0, bad)


class UnpackGolden(unittest.TestCase):
    def test_rc3_with_data(self):
        self.assertEqual(unpack(0x000000030004A02D), Response(rc=3, data=0x0004A02D))

    def test_rc5(self):
        self.assertEqual(unpack(0x0000000500000000).rc, 5)

    def test_response_is_frozen(self):
        with self.assertRaises(Exception):
            unpack(0).rc = 1  # type: ignore[misc]

    def test_busy_bit_never_leaks_into_rc_or_data(self):
        r = unpack(0x8000000000000000)
        self.assertEqual((r.rc, r.data), (0, 0))
        r = unpack(0x8000001100000000 | 0x0004A02D)  # a still-busy request echoed back
        self.assertEqual((r.rc, r.data), (0x11, 0x0004A02D))
        r = unpack(0xFFFFFFFFFFFFFFFF)
        self.assertEqual((r.rc, r.data), (0xFF, 0xFFFFFFFF))

    def test_bits_above_rc_do_not_leak_into_rc(self):
        self.assertEqual(unpack(0x7FFF_FF00_0000_0000).rc, 0)
        self.assertEqual(unpack(0x7FFF_FF03_0000_0000).rc, 3)

    def test_pack_then_unpack_roundtrips_cmd_as_rc_field(self):
        raw = pack(0x11, 0, 0, 0x0004A02D)
        self.assertEqual(unpack(raw), Response(rc=0x11, data=0x0004A02D))

    def test_refuses_out_of_range_raw(self):
        for bad in (-1, 2**64, 1.0, "0", None, True):
            with self.subTest(raw=bad), self.assertRaises(ValueError):
                unpack(bad)


class IsBusy(unittest.TestCase):
    def test_bit63_set(self):
        self.assertTrue(is_busy(0x8000000000000000))

    def test_bit63_clear(self):
        self.assertFalse(is_busy(0x7FFFFFFFFFFFFFFF))

    def test_zero_and_all_ones(self):
        self.assertFalse(is_busy(0))
        self.assertTrue(is_busy(0xFFFFFFFFFFFFFFFF))

    def test_pack_output_is_busy(self):
        self.assertTrue(is_busy(pack(0x10, 0, 0, 0)))

    def test_refuses_out_of_range_raw(self):
        for bad in (-1, 2**64, 0.5, None, False):
            with self.subTest(raw=bad), self.assertRaises(ValueError):
                is_busy(bad)


class PackUnpackProperty(unittest.TestCase):
    def test_random_fields_roundtrip(self):
        rng = random.Random(0x285)
        for _ in range(2000):
            cmd, p1, p2 = (rng.randrange(256) for _ in range(3))
            data = rng.randrange(2**32)
            raw = pack(cmd, p1, p2, data)
            self.assertTrue(is_busy(raw))
            self.assertEqual(unpack(raw), Response(rc=cmd, data=data))
            self.assertEqual((raw >> 40) & 0xFF, p1)
            self.assertEqual((raw >> 48) & 0xFF, p2)


class Purity(unittest.TestCase):
    """T-010 acceptance: the codec is pure (stdlib only, no I/O, no print)."""

    ALLOWED_IMPORTS = {"__future__", "dataclasses", "enum", "fractions", "math", "typing"}
    FORBIDDEN_NAMES = {"print", "open", "input", "exec", "eval", "__import__", "compile"}

    def test_modules_import_only_stdlib_and_each_other(self):
        for mod in (words, vf, commands):
            tree = ast.parse(inspect.getsource(mod))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    roots = set() if node.level else {(node.module or "").split(".")[0]}
                else:
                    continue
                with self.subTest(module=mod.__name__, imports=sorted(roots)):
                    self.assertLessEqual(roots, self.ALLOWED_IMPORTS)

    def test_modules_call_no_io_builtins(self):
        for mod in (words, vf, commands):
            tree = ast.parse(inspect.getsource(mod))
            called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            with self.subTest(module=mod.__name__):
                self.assertFalse(called & self.FORBIDDEN_NAMES, called & self.FORBIDDEN_NAMES)


if __name__ == "__main__":
    unittest.main()
