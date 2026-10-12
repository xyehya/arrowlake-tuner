"""Tests for arltuner.topology (T-012). Fake sysfs = temp directories; the real /sys is never read."""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import inspect
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import arltuner.topology as topology_pkg
from arltuner.topology import (
    Core,
    Topology,
    TopologyError,
    TopologyMismatch,
    UnknownCore,
    arl_s_285k,
    contract_dict,
    detect,
    read_live,
    synthetic,
    topology_problems,
)
from tests.fakesys import make_sysfs, reference_like


class StaticMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.topo = arl_s_285k()

    def test_24_cores_8p_16e(self) -> None:
        self.assertEqual(len(self.topo.cores), 24)
        self.assertEqual([c.kind for c in self.topo.cores], ["P"] * 8 + ["E"] * 16)

    def test_p_cores_map_index_to_same_cpu(self) -> None:
        for i in range(8):
            c = self.topo.resolve("P", i)
            self.assertEqual((c.index, c.cpu, c.cluster), (i, i, None))

    def test_favored_is_exactly_p0_and_p3(self) -> None:
        favored = [(c.kind, c.index) for c in self.topo.cores if c.favored]
        self.assertEqual(favored, [("P", 0), ("P", 3)])

    def test_e_cores_map_to_cpu_8_plus_index_and_cluster(self) -> None:
        for k in range(16):
            c = self.topo.resolve("E", k)
            self.assertEqual((c.cpu, c.cluster, c.favored), (8 + k, k // 4, False))

    def test_spec_vectors(self) -> None:
        self.assertEqual(self.topo.resolve("P", 4).cpu, 4)
        e12 = self.topo.resolve("E", 12)
        self.assertEqual((e12.cpu, e12.cluster), (20, 3))
        self.assertEqual(self.topo.cluster_cpus(3), (20, 21, 22, 23))
        by9 = self.topo.by_cpu(9)
        self.assertEqual((by9.kind, by9.index), ("E", 1))
        self.assertEqual(self.topo.xtu_name(self.topo.resolve("P", 4)), "P-Core 4")
        self.assertEqual(self.topo.xtu_name(e12), "E-Core 12")

    def test_cluster_cpus_all_clusters(self) -> None:
        self.assertEqual(
            [self.topo.cluster_cpus(n) for n in range(4)],
            [(8, 9, 10, 11), (12, 13, 14, 15), (16, 17, 18, 19), (20, 21, 22, 23)],
        )

    def test_by_cpu_and_resolve_round_trip(self) -> None:
        for c in self.topo.cores:
            self.assertIs(self.topo.by_cpu(c.cpu), c)
            self.assertIs(self.topo.resolve(c.kind, c.index), c)

    def test_unknown_lookups_raise_unknown_core(self) -> None:
        bad_calls = [
            lambda: self.topo.resolve("X", 0),
            lambda: self.topo.resolve("P", 8),
            lambda: self.topo.resolve("E", 16),
            lambda: self.topo.resolve("P", -1),
            lambda: self.topo.resolve("P", True),  # bool is not an index
            lambda: self.topo.by_cpu(24),
            lambda: self.topo.by_cpu(-1),
            lambda: self.topo.cluster_cpus(4),
            lambda: self.topo.cluster_cpus(-1),
        ]
        for call in bad_calls:
            with self.subTest(call=inspect.getsource(call).strip()):
                with self.assertRaises(UnknownCore):
                    call()

    def test_unknown_core_is_keyerror_and_valueerror_with_clear_message(self) -> None:
        with self.assertRaises(KeyError):
            self.topo.by_cpu(99)
        with self.assertRaises(ValueError):
            self.topo.by_cpu(99)
        with self.assertRaises(TopologyError):
            self.topo.by_cpu(99)
        try:
            self.topo.by_cpu(99)
        except UnknownCore as exc:
            self.assertEqual(str(exc), "unknown Linux CPU 99")

    def test_xtu_name_rejects_foreign_core(self) -> None:
        foreign = Core(kind="E", index=40, cpu=99, cluster=9, favored=False)
        with self.assertRaises(UnknownCore):
            self.topo.xtu_name(foreign)
        # same key but different attributes is also not "this" core
        with self.assertRaises(UnknownCore):
            self.topo.xtu_name(dataclasses.replace(self.topo.resolve("P", 0), favored=False))

    def test_immutable_and_equal(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.topo.cores = ()  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.topo.cores[0].cpu = 5  # type: ignore[misc]
        self.assertEqual(self.topo, arl_s_285k())
        self.assertEqual(hash(self.topo), hash(arl_s_285k()))
        self.assertNotEqual(self.topo, Topology(cores=self.topo.cores[:8]))

    def test_topology_rejects_duplicates(self) -> None:
        p0 = self.topo.resolve("P", 0)
        with self.assertRaises(ValueError):
            Topology(cores=(p0, p0))
        with self.assertRaises(ValueError):
            Topology(cores=(p0, dataclasses.replace(p0, cpu=1)))  # same (kind, index)

    def test_core_validates_kind_and_cluster(self) -> None:
        with self.assertRaises(ValueError):
            Core(kind="X", index=0, cpu=0, cluster=None, favored=False)
        with self.assertRaises(ValueError):
            Core(kind="P", index=0, cpu=0, cluster=0, favored=False)
        with self.assertRaises(ValueError):
            Core(kind="E", index=0, cpu=8, cluster=None, favored=False)


class DetectTests(unittest.TestCase):
    """``detect`` / ``read_live`` over fake sysfs trees (G-128, D-036): everything is derived live."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def sub(self, name: str) -> Path:
        return self.root / name

    def test_8p_16e_cppc_favored_0_and_3(self) -> None:
        reference_like(self.sub("a"))
        live = read_live(self.sub("a"))
        self.assertEqual(live.topology, arl_s_285k())
        self.assertEqual(live.favored_source, "acpi_cppc")
        self.assertEqual([u.id for u in live.units], [f"P{i}" for i in range(8)] + [f"EC{i}" for i in range(4)])
        self.assertEqual([u.id for u in live.units if u.favored], ["P0", "P3"])
        ec1 = next(u for u in live.units if u.id == "EC1")
        self.assertEqual((ec1.kind, ec1.cpus, ec1.max_ratio, ec1.perf, ec1.favored), ("E", (12, 13, 14, 15), 49, 68, False))
        p0 = live.units[0]
        self.assertEqual((p0.cpus, p0.max_ratio, p0.perf), ((0,), 57, 92))

    def test_8p_12e(self) -> None:
        reference_like(self.sub("a"), favored=(1,), e=12)
        live = read_live(self.sub("a"))
        self.assertEqual(len(live.topology.cores), 20)
        self.assertEqual([u.id for u in live.units][-3:], ["EC0", "EC1", "EC2"])
        self.assertEqual([u.id for u in live.units if u.favored], ["P1"])
        self.assertEqual(live.topology.cluster_cpus(2), (16, 17, 18, 19))

    def test_6p_8e(self) -> None:
        reference_like(self.sub("a"), favored=(2, 4), p=6, e=8)
        live = read_live(self.sub("a"))
        self.assertEqual([u.id for u in live.units], [f"P{i}" for i in range(6)] + ["EC0", "EC1"])
        self.assertEqual([u.id for u in live.units if u.favored], ["P2", "P4"])
        self.assertEqual(live.topology.cpus_of_kind("E"), tuple(range(6, 14)))
        self.assertEqual(live.topology.resolve("E", 3).cpu, 9)

    def test_all_equal_means_no_favored(self) -> None:
        reference_like(self.sub("a"), favored=())
        live = read_live(self.sub("a"))
        self.assertIsNone(live.favored_source)
        self.assertFalse(any(u.favored for u in live.units))
        self.assertFalse(any(c.favored for c in live.topology.cores))

    def test_cppc_missing_falls_back_to_cpuinfo_max_freq(self) -> None:
        reference_like(self.sub("a"), cppc=False)
        live = read_live(self.sub("a"))
        self.assertEqual(live.favored_source, "cpuinfo_max_freq")
        self.assertEqual([u.id for u in live.units if u.favored], ["P0", "P3"])
        self.assertTrue(all(u.perf is None for u in live.units))

    def test_nothing_readable_means_no_favored_and_no_numbers(self) -> None:
        make_sysfs(self.sub("a"), 4, 4)
        live = read_live(self.sub("a"))
        self.assertIsNone(live.favored_source)
        self.assertTrue(all(u.max_ratio is None and u.perf is None and not u.favored for u in live.units))

    def test_contract_shape(self) -> None:
        reference_like(self.sub("a"))
        contract = read_live(self.sub("a")).contract()
        self.assertEqual(set(contract), {"units", "favored_source"})
        self.assertEqual(contract["units"][0], {"id": "P0", "kind": "P", "cpus": [0], "favored": True, "max_ratio": 57, "perf": 92})
        self.assertEqual(contract["units"][8], {"id": "EC0", "kind": "E", "cpus": [8, 9, 10, 11], "favored": False, "max_ratio": 49, "perf": 68})
        self.assertEqual(contract_dict(arl_s_285k())["favored_source"], "acpi_cppc")

    def test_detect_returns_topology_and_accepts_str_root(self) -> None:
        reference_like(self.sub("a"))
        self.assertEqual(detect(self.sub("a")), arl_s_285k())
        self.assertEqual(detect(str(self.sub("a"))), arl_s_285k())  # type: ignore[arg-type]
        self.assertEqual(detect(self.sub("a")).favored_source, "acpi_cppc")

    def test_max_ratio_of_kind(self) -> None:
        reference_like(self.sub("a"))
        live = read_live(self.sub("a"))
        self.assertEqual((live.max_ratio_of_kind("P"), live.max_ratio_of_kind("E")), (57, 49))

    def test_missing_files_raise(self) -> None:
        make_sysfs(self.sub("a"), 8, 16)
        (self.sub("a") / "devices" / "cpu_core" / "cpus").unlink()
        make_sysfs(self.sub("b"), 8, 16)
        (self.sub("b") / "devices" / "cpu_atom" / "cpus").unlink()
        make_sysfs(self.sub("c"), 8, 16)
        (self.sub("c") / "devices" / "system" / "cpu" / "cpu9" / "topology" / "cluster_cpus_list").unlink()
        for name in "abc":
            with self.subTest(name):
                with self.assertRaises(TopologyMismatch):
                    detect(self.sub(name))

    def test_nonexistent_root_raises(self) -> None:
        with self.assertRaises(TopologyMismatch):
            detect(self.root / "does-not-exist")

    def test_malformed_cpulists_raise(self) -> None:
        bad = ["", "\n", "abc", "0-", "-7", "7-0", "0-7x", "0,,1", "0 - 7", "0-7,", ",0-7", "0-7;8", "0-7\n8-9", "0-99999"]
        for i, text in enumerate(bad):
            for which in ("cpu_core", "cpu_atom"):
                with self.subTest(which=which, text=text):
                    root = make_sysfs(self.sub(f"{which}{i}"), 8, 16)
                    (root / "devices" / which / "cpus").write_text(text, encoding="ascii")
                    with self.assertRaises(TopologyMismatch):
                        detect(root)

    def test_duplicate_and_overlapping_cpus_raise(self) -> None:
        root = make_sysfs(self.sub("dup"), 8, 16)
        (root / "devices" / "cpu_core" / "cpus").write_text("0-7,3\n", encoding="ascii")
        with self.assertRaises(TopologyMismatch):
            detect(root)
        root = make_sysfs(self.sub("overlap"), 8, 16)
        (root / "devices" / "cpu_atom" / "cpus").write_text("7-23\n", encoding="ascii")
        with self.assertRaises(TopologyMismatch):
            detect(root)

    def test_non_ascii_content_raises(self) -> None:
        root = make_sysfs(self.sub("a"), 8, 16)
        (root / "devices" / "cpu_core" / "cpus").write_bytes(b"\xff\xfe0-7")
        with self.assertRaises(TopologyMismatch):
            detect(root)

    def test_inconsistent_cluster_lists_raise(self) -> None:
        root = make_sysfs(self.sub("a"), 8, 16)
        (root / "devices" / "system" / "cpu" / "cpu9" / "topology" / "cluster_cpus_list").write_text("9-12\n", encoding="ascii")
        with self.assertRaises(TopologyMismatch):
            detect(root)
        root = make_sysfs(self.sub("b"), 8, 16)
        (root / "devices" / "system" / "cpu" / "cpu8" / "topology" / "cluster_cpus_list").write_text("0-3\n", encoding="ascii")
        with self.assertRaises(TopologyMismatch):
            detect(root)

    def test_mismatch_is_topology_error_and_message_names_the_file(self) -> None:
        root = make_sysfs(self.sub("a"), 8, 16)
        (root / "devices" / "cpu_core" / "cpus").write_text("zz", encoding="ascii")
        with self.assertRaises(TopologyError) as cm:
            detect(root)
        self.assertIn("cpu_core", str(cm.exception))

    def test_problems_empty_sets_and_unequal_clusters(self) -> None:
        self.assertEqual(topology_problems(arl_s_285k()), [])
        self.assertEqual(topology_problems(synthetic(6, 8)), [])
        unequal = Topology(
            tuple(synthetic(2, 4).cores) + (Core("E", 4, 6, 1, False),) + (Core("E", 5, 7, 1, False),) + (Core("E", 6, 8, 2, False),)
        )
        self.assertTrue(any("unequal" in p for p in topology_problems(unequal)))
        no_e = Topology(tuple(c for c in synthetic(4, 4).cores if c.kind == "P"))
        self.assertTrue(any("no E-core" in p for p in topology_problems(no_e)))

    def test_default_root_is_real_sys_but_is_not_used_here(self) -> None:
        # Only inspects the signature; the real /sys is never read by this test suite.
        self.assertEqual(inspect.signature(detect).parameters["sysfs_root"].default, Path("/sys"))
        self.assertEqual(inspect.signature(read_live).parameters["root"].default, Path("/sys"))

    def test_unit_helpers(self) -> None:
        topo = synthetic(6, 8)
        self.assertEqual(topo.unit_ids(), ("P0", "P1", "P2", "P3", "P4", "P5", "EC0", "EC1"))
        self.assertEqual(topo.unit_cpus("EC1"), (10, 11, 12, 13))
        self.assertEqual(topo.unit_cpus("P4"), (4,))
        self.assertEqual(topo.unit_of_cpu(7), "EC0")
        for bad in ("P6", "EC2", "X", "P", "EC"):
            with self.assertRaises(UnknownCore):
                topo.unit_cpus(bad)


class ImportTests(unittest.TestCase):
    def test_import_has_no_side_effects(self) -> None:
        # Execute a private copy of the module (the real one stays untouched in sys.modules, so class
        # identities used by other tests are not disturbed) with file access and stdout trapped.
        path = Path(inspect.getsourcefile(topology_pkg.Topology) or "")
        name = "_arltuner_cpumap_import_probe"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        out = io.StringIO()
        sys.modules[name] = module
        try:
            with (
                contextlib.redirect_stdout(out),
                mock.patch("builtins.open", side_effect=AssertionError("open() at import")),
                mock.patch.object(Path, "open", side_effect=AssertionError("Path.open() at import")),
                mock.patch.object(Path, "read_text", side_effect=AssertionError("read_text() at import")),
            ):
                spec.loader.exec_module(module)
        finally:
            del sys.modules[name]
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
