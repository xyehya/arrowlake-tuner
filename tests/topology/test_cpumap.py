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
    detect,
)


def make_sysfs(root: Path, cpu_core: str | None, cpu_atom: str | None) -> Path:
    """Write a fake sysfs tree; ``None`` leaves the file missing."""
    for name, text in (("cpu_core", cpu_core), ("cpu_atom", cpu_atom)):
        if text is not None:
            d = root / "devices" / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "cpus").write_text(text, encoding="ascii")
    return root


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
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_matching_sysfs_returns_static_topology(self) -> None:
        make_sysfs(self.root, "0-7\n", "8-23\n")
        self.assertEqual(detect(self.root), arl_s_285k())

    def test_cpulist_variants_that_match(self) -> None:
        variants = [
            ("0-7", "8-23"),  # no trailing newline
            ("0-7\n", "8-11,12-23\n"),
            ("0-3,4-7\n", "8-23\n"),
            ("0,1,2,3,4,5,6,7\n", "8-10,11-23\n"),
            ("  0-7  \n", "8-23\n"),
        ]
        for p, e in variants:
            with self.subTest(cpu_core=p, cpu_atom=e):
                root = make_sysfs(self.root / f"v{abs(hash((p, e)))}", p, e)
                self.assertEqual(detect(root), arl_s_285k())

    def test_accepts_str_root(self) -> None:
        make_sysfs(self.root, "0-7\n", "8-23\n")
        self.assertEqual(detect(str(self.root)), arl_s_285k())  # type: ignore[arg-type]

    def test_missing_files_raise(self) -> None:
        cases = {
            "both missing": (None, None),
            "cpu_core missing": (None, "8-23\n"),
            "cpu_atom missing": ("0-7\n", None),
        }
        for label, (p, e) in cases.items():
            with self.subTest(label):
                root = make_sysfs(self.root / label.replace(" ", "_"), p, e)
                root.mkdir(parents=True, exist_ok=True)
                with self.assertRaises(TopologyMismatch):
                    detect(root)

    def test_nonexistent_root_raises(self) -> None:
        with self.assertRaises(TopologyMismatch):
            detect(self.root / "does-not-exist")

    def test_malformed_cpulists_raise(self) -> None:
        bad = ["", "\n", "abc", "0-", "-7", "7-0", "0-7x", "0,,1", "0 - 7", "0-7,", ",0-7", "0-7;8", "0-7\n8-9", "0-99999"]
        for text in bad:
            with self.subTest(cpu_core=text):
                root = make_sysfs(self.root / f"p{abs(hash(text))}", text, "8-23\n")
                with self.assertRaises(TopologyMismatch):
                    detect(root)
            with self.subTest(cpu_atom=text):
                root = make_sysfs(self.root / f"e{abs(hash(text))}", "0-7\n", text)
                with self.assertRaises(TopologyMismatch):
                    detect(root)

    def test_duplicate_cpus_in_list_raise(self) -> None:
        make_sysfs(self.root, "0-7,3\n", "8-23\n")
        with self.assertRaises(TopologyMismatch):
            detect(self.root)

    def test_non_ascii_content_raises(self) -> None:
        d = self.root / "devices" / "cpu_core"
        d.mkdir(parents=True)
        (d / "cpus").write_bytes(b"\xff\xfe0-7")
        make_sysfs(self.root, None, "8-23\n")
        with self.assertRaises(TopologyMismatch):
            detect(self.root)

    def test_different_topologies_raise(self) -> None:
        cases = {
            "p short": ("0-6\n", "8-23\n"),
            "p extra": ("0-8\n", "8-23\n"),
            "e short": ("0-7\n", "8-22\n"),
            "e extra": ("0-7\n", "8-24\n"),
            "swapped": ("8-23\n", "0-7\n"),
            "smt-like": ("0-15\n", "16-31\n"),
            "overlap": ("0-7\n", "0-23\n"),
            "gap": ("0-7\n", "8-11,16-23\n"),
            "older hybrid 8p+8e": ("0-7\n", "8-15\n"),
        }
        for label, (p, e) in cases.items():
            with self.subTest(label):
                root = make_sysfs(self.root / label.replace(" ", "_"), p, e)
                with self.assertRaises(TopologyMismatch):
                    detect(root)

    def test_mismatch_is_topology_error_and_message_names_the_file(self) -> None:
        make_sysfs(self.root, "0-6\n", "8-23\n")
        with self.assertRaises(TopologyError) as cm:
            detect(self.root)
        self.assertIn("cpu_core", str(cm.exception))

    def test_default_root_is_real_sys_but_is_not_used_here(self) -> None:
        # Only inspects the signature; the real /sys is never read by this test suite.
        self.assertEqual(inspect.signature(detect).parameters["sysfs_root"].default, Path("/sys"))


class ImportTests(unittest.TestCase):
    def test_import_has_no_side_effects(self) -> None:
        # Execute a private copy of the module (the real one stays untouched in sys.modules, so class
        # identities used by other tests are not disturbed) with file access and stdout trapped.
        path = Path(inspect.getsourcefile(topology_pkg.arl_s_285k) or "")
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
