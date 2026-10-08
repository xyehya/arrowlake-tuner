"""T-015 ``diff`` tests: what makes a health delta CLEAN (docs/tasks/T-015-health-gate.md, docs/SAFETY.md).

Snapshots are built directly (no filesystem): the verdict is a pure function of two ``HealthSnapshot`` values.
"""
from __future__ import annotations

import unittest

from arltuner.health import HealthDelta, HealthError, HealthSnapshot, decode_bits, diff

PCI = frozenset({"0000:00:00.0", "0000:00:1f.6", "0000:2b:00.0"})
MODULES = frozenset({"snd_hda_intel", "i915", "msr"})
CPU_OUT_OF_SPEC_LIKE = 29  # an arbitrary example bit index; nothing in the code knows which bit means what


def snap(taint: int = 0, pci: frozenset[str] = PCI, modules: frozenset[str] = MODULES, *, thermal=None, mc=None, at: float = 1.0) -> HealthSnapshot:
    return HealthSnapshot(at, taint, decode_bits(taint), pci, modules, thermal, mc)


class Clean(unittest.TestCase):
    def test_identical_snapshots(self) -> None:
        d = diff(snap(4), snap(4))
        self.assertIsInstance(d, HealthDelta)
        self.assertTrue(d.clean)
        self.assertFalse(d.taint_changed)
        self.assertEqual(
            (d.new_taint_bits, d.cleared_taint_bits, d.pci_lost, d.pci_added, d.modules_added, d.modules_removed),
            ((), (), (), (), (), ()),
        )
        self.assertTrue(d.summary.startswith("CLEAN"), d.summary)

    def test_a_nonzero_baseline_is_compared_not_zero(self) -> None:
        self.assertTrue(diff(snap(1 << 29), snap(1 << 29)).clean)  # earlier-tool taint already in the boot word
        self.assertTrue(diff(snap((1 << 29) | 4), snap((1 << 29) | 4)).clean)

    def test_time_and_informational_fields_never_matter(self) -> None:
        a = snap(0, thermal={"thermal_zone0": 40000}, mc={"machinecheck0/bank0": 1}, at=1.0)
        b = snap(0, thermal={"thermal_zone0": 99000, "thermal_zone1": 5}, mc={"machinecheck0/bank0": 2}, at=500.0)
        self.assertTrue(diff(a, b).clean)
        self.assertTrue(diff(a, snap(0)).clean)  # present -> absent
        self.assertTrue(diff(snap(0), b).clean)  # absent -> present

    def test_pci_and_module_order_is_irrelevant(self) -> None:
        self.assertTrue(diff(snap(0, frozenset(sorted(PCI)), frozenset(sorted(MODULES))), snap(0, frozenset(reversed(sorted(PCI))))).clean)


class Taint(unittest.TestCase):
    def test_new_bit_is_unclean(self) -> None:
        d = diff(snap(0), snap(1 << CPU_OUT_OF_SPEC_LIKE))
        self.assertFalse(d.clean)
        self.assertTrue(d.taint_changed)
        self.assertEqual(d.new_taint_bits, (CPU_OUT_OF_SPEC_LIKE,))
        self.assertEqual(d.cleared_taint_bits, ())
        self.assertIn(str(CPU_OUT_OF_SPEC_LIKE), d.summary)
        self.assertTrue(d.summary.startswith("UNCLEAN"), d.summary)

    def test_new_bit_on_top_of_an_existing_baseline(self) -> None:
        d = diff(snap(4), snap(4 | (1 << 29)))
        self.assertFalse(d.clean)
        self.assertEqual(d.new_taint_bits, (29,))

    def test_allowed_new_bit_is_clean_but_still_reported(self) -> None:
        d = diff(snap(0), snap(1 << 29), allowed_new_taint_bits=frozenset({29}))
        self.assertTrue(d.clean)
        self.assertTrue(d.taint_changed)
        self.assertEqual(d.new_taint_bits, (29,))
        self.assertIn("allowed", d.summary)

    def test_a_different_new_bit_is_still_unclean(self) -> None:
        d = diff(snap(0), snap(1 << 30), allowed_new_taint_bits=frozenset({29}))
        self.assertFalse(d.clean)
        self.assertEqual(d.new_taint_bits, (30,))

    def test_allowed_bit_plus_an_unexpected_one_is_unclean(self) -> None:
        d = diff(snap(0), snap((1 << 29) | (1 << 7)), allowed_new_taint_bits=frozenset({29}))
        self.assertFalse(d.clean)
        self.assertEqual(d.new_taint_bits, (7, 29))
        self.assertIn("7", d.summary)

    def test_allowed_set_does_not_excuse_a_cleared_bit(self) -> None:
        d = diff(snap(1 << 29), snap(0), allowed_new_taint_bits=frozenset({29}))
        self.assertFalse(d.clean)
        self.assertEqual(d.cleared_taint_bits, (29,))
        self.assertEqual(d.new_taint_bits, ())

    def test_cleared_bit_is_unclean(self) -> None:
        d = diff(snap(4 | 8), snap(4))
        self.assertFalse(d.clean)
        self.assertTrue(d.taint_changed)
        self.assertEqual(d.cleared_taint_bits, (3,))
        self.assertIn("cleared", d.summary)

    def test_bit_moved_is_both_new_and_cleared(self) -> None:
        d = diff(snap(4), snap(8), allowed_new_taint_bits=frozenset({3}))
        self.assertFalse(d.clean)
        self.assertEqual((d.new_taint_bits, d.cleared_taint_bits), ((3,), (2,)))

    def test_allowed_bits_validation(self) -> None:
        for bad in ({-1}, {True}, {"29"}, {1.5}, 5):
            with self.subTest(bad=bad):
                with self.assertRaises(HealthError):
                    diff(snap(0), snap(0), allowed_new_taint_bits=bad)  # type: ignore[arg-type]

    def test_allowed_bits_may_be_any_iterable_of_ints(self) -> None:
        self.assertTrue(diff(snap(0), snap(1 << 29), allowed_new_taint_bits=[29]).clean)  # type: ignore[arg-type]

    def test_summary_shows_bit_indices_not_letters(self) -> None:
        d = diff(snap(0), snap(1 << 29))
        self.assertNotIn("OUT_OF_SPEC", d.summary.upper())


class Pci(unittest.TestCase):
    def test_lost_device(self) -> None:
        d = diff(snap(), snap(pci=PCI - {"0000:2b:00.0"}))
        self.assertFalse(d.clean)
        self.assertEqual((d.pci_lost, d.pci_added), (("0000:2b:00.0",), ()))
        self.assertIn("0000:2b:00.0", d.summary)
        self.assertFalse(d.taint_changed)

    def test_added_device(self) -> None:
        d = diff(snap(), snap(pci=PCI | {"0000:2c:00.0"}))
        self.assertFalse(d.clean)
        self.assertEqual((d.pci_lost, d.pci_added), ((), ("0000:2c:00.0",)))

    def test_swapped_device_is_both(self) -> None:
        d = diff(snap(), snap(pci=(PCI - {"0000:00:1f.6"}) | {"0000:00:1f.7"}))
        self.assertFalse(d.clean)
        self.assertEqual((d.pci_lost, d.pci_added), (("0000:00:1f.6",), ("0000:00:1f.7",)))


class Modules(unittest.TestCase):
    def test_added_module(self) -> None:
        d = diff(snap(), snap(modules=MODULES | {"vfio"}))
        self.assertFalse(d.clean)
        self.assertEqual((d.modules_added, d.modules_removed), (("vfio",), ()))
        self.assertIn("vfio", d.summary)

    def test_removed_module(self) -> None:
        d = diff(snap(), snap(modules=MODULES - {"msr"}))
        self.assertFalse(d.clean)
        self.assertEqual((d.modules_added, d.modules_removed), ((), ("msr",)))

    def test_results_are_sorted_tuples(self) -> None:
        d = diff(snap(), snap(modules=MODULES | {"zz", "aa", "mm"}))
        self.assertEqual(d.modules_added, ("aa", "mm", "zz"))

    def test_long_lists_are_truncated_in_the_summary_only(self) -> None:
        extra = frozenset(f"mod{i:03d}" for i in range(50))
        d = diff(snap(), snap(modules=MODULES | extra))
        self.assertEqual(len(d.modules_added), 50)
        self.assertIn("+42 more", d.summary)
        self.assertLess(len(d.summary), 400)


class Inputs(unittest.TestCase):
    def test_needs_two_snapshots(self) -> None:
        with self.assertRaises(HealthError):
            diff(snap(), {"taint": 0})  # type: ignore[arg-type]
        with self.assertRaises(HealthError):
            diff(None, snap())  # type: ignore[arg-type]

    def test_delta_is_frozen(self) -> None:
        d = diff(snap(), snap())
        with self.assertRaises(Exception):
            d.clean = False  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
