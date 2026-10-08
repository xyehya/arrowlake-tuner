"""T-015 snapshot tests: parsing, fail-closed mandatory files, reads confined to ``root``, JSON persistence.

Facts: docs/tasks/T-015-health-gate.md; rules: docs/SAFETY.md "Mailbox-session discipline". Every test uses a fake root under a
temp dir (``tests/health/fakeroot.py``); nothing reads the real /proc or /sys.
"""
from __future__ import annotations

import builtins
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arltuner.health import HealthError, HealthSnapshot, decode_bits, load_snapshot, save_snapshot, take_snapshot

from .fakeroot import DEFAULT_MODULES, DEFAULT_PCI, FakeRoot, FakeRootCase


class RootCase(FakeRootCase):
    def snap(self, **kwargs: object) -> HealthSnapshot:
        return take_snapshot(self.root.path, clock=lambda: 1234.5, **kwargs)  # type: ignore[arg-type]


class DecodeBits(unittest.TestCase):
    def test_zero_is_empty(self) -> None:
        self.assertEqual(decode_bits(0), ())

    def test_indices_ascending(self) -> None:
        self.assertEqual(decode_bits(1), (0,))
        self.assertEqual(decode_bits(4), (2,))
        self.assertEqual(decode_bits(0b101010), (1, 3, 5))
        self.assertEqual(decode_bits(1 << 29), (29,))
        self.assertEqual(decode_bits((1 << 63) | 1), (0, 63))
        self.assertEqual(decode_bits((1 << 64) - 1), tuple(range(64)))

    def test_rejects_non_integers_and_negatives(self) -> None:
        for bad in (-1, True, 1.0, "4", None):
            with self.subTest(bad=bad):
                with self.assertRaises(HealthError):
                    decode_bits(bad)  # type: ignore[arg-type]


class TakeSnapshot(RootCase):
    def test_parses_the_mandatory_facts(self) -> None:
        s = self.snap()
        self.assertEqual(s.taken_at, 1234.5)
        self.assertEqual((s.taint, s.taint_bits), (0, ()))
        self.assertEqual(s.pci, frozenset(DEFAULT_PCI))
        self.assertEqual(s.modules, frozenset(DEFAULT_MODULES))
        self.assertIsNone(s.thermal_mc)
        self.assertIsNone(s.machinecheck)

    def test_taint_values(self) -> None:
        cases = {"0": (), "4": (2,), "536870912": (29,), "4611686018427387904": (62,), str((1 << 64) - 1): tuple(range(64))}
        for text, bits in cases.items():
            with self.subTest(taint=text):
                self.root.set_taint(text)
                s = self.snap()
                self.assertEqual((s.taint, s.taint_bits), (int(text), bits))

    def test_taint_without_trailing_newline_and_with_spaces(self) -> None:
        self.root.tainted.write_bytes(b"  12  ")
        self.assertEqual(self.snap().taint_bits, (2, 3))

    def test_garbage_taint_is_an_error(self) -> None:
        for text in ("", "\n", "abc", "0x10", "-1", "1 2", "4.0", "١٢", "9" * 41):
            with self.subTest(text=text):
                self.root.tainted.write_bytes(text.encode("utf-8"))
                with self.assertRaises(HealthError):
                    self.snap()

    def test_pci_names_are_the_directory_entries(self) -> None:
        self.root.add_pci("10000:ff:1f.7")
        s = self.snap()
        self.assertEqual(s.pci, frozenset(DEFAULT_PCI) | {"10000:ff:1f.7"})
        self.assertIn("0000:00:1f.6", s.pci)  # genuine PCI address text, colons included

    def test_modules_use_only_the_first_column(self) -> None:
        self.root.modules_file.write_text(
            "snd_hda_intel 61440 3 snd_hda_codec, Live 0xffffffffc0a00000\n"
            "\n"
            "msr 12288 0 - Live 0x0000000000000000\n"
            "   \n"
            "bare_name\n"
            "snd_hda_intel 1 1 - Live 0x0\n",  # a repeated name is a set member once
            encoding="ascii",
        )
        self.assertEqual(self.snap().modules, frozenset({"snd_hda_intel", "msr", "bare_name"}))

    def test_empty_module_list_is_allowed(self) -> None:
        self.root.modules_file.write_text("", encoding="ascii")
        self.assertEqual(self.snap().modules, frozenset())

    def test_thermal_present(self) -> None:
        self.root.set_thermal({"thermal_zone0": 45000, "thermal_zone1": "-5000", "thermal_zone12": 98250})
        self.assertEqual(self.snap().thermal_mc, {"thermal_zone0": 45000, "thermal_zone1": -5000, "thermal_zone12": 98250})

    def test_thermal_unreadable_zones_are_skipped_not_errors(self) -> None:
        self.root.set_thermal({"thermal_zone0": "not a number", "thermal_zone1": 40000})
        (self.root.path / "sys" / "class" / "thermal" / "thermal_zone2").mkdir()  # a zone without a temp file
        self.assertEqual(self.snap().thermal_mc, {"thermal_zone1": 40000})

    def test_thermal_with_nothing_readable_is_none(self) -> None:
        self.root.set_thermal({"thermal_zone0": ""})
        self.assertIsNone(self.snap().thermal_mc)

    def test_machinecheck_present(self) -> None:
        self.root.set_machinecheck({"machinecheck0/bank0": "ffffffffffffffff", "machinecheck1/bank2": "0x10", "machinecheck1/bank3": "zz"})
        self.assertEqual(
            self.snap().machinecheck,
            {"machinecheck0/bank0": (1 << 64) - 1, "machinecheck1/bank2": 16},
        )

    def test_machinecheck_absent_is_none(self) -> None:
        self.assertIsNone(self.snap().machinecheck)

    def test_each_missing_mandatory_fact_is_an_error(self) -> None:
        cases = {
            "tainted": lambda r: r.tainted.unlink(),
            "pci dir": lambda r: shutil.rmtree(r.pci_dir),
            "modules": lambda r: r.modules_file.unlink(),
        }
        for label, break_it in cases.items():
            with self.subTest(label):
                root = FakeRoot(Path(tempfile.mkdtemp(dir=self.dir)))
                break_it(root)
                with self.assertRaises(HealthError):
                    take_snapshot(root.path)

    def test_a_directory_where_a_file_belongs_is_an_error(self) -> None:
        self.root.tainted.unlink()
        self.root.tainted.mkdir()
        with self.assertRaises(HealthError):
            self.snap()

    def test_empty_pci_directory_is_an_error_not_a_clean_negative(self) -> None:
        for addr in DEFAULT_PCI:
            self.root.remove_pci(addr)
        with self.assertRaises(HealthError) as cm:
            self.snap()
        self.assertIn("empty", str(cm.exception))

    def test_oversized_file_is_refused_not_truncated(self) -> None:
        self.root.modules_file.write_bytes(b"x 1 0 - Live 0x0\n" * (300_000))  # ~5 MB
        with self.assertRaises(HealthError):
            self.snap()

    def test_clock_is_used_and_must_return_a_number(self) -> None:
        self.assertEqual(take_snapshot(self.root.path, clock=lambda: 7).taken_at, 7.0)
        with self.assertRaises(HealthError):
            take_snapshot(self.root.path, clock=lambda: "now")  # type: ignore[arg-type, return-value]
        with self.assertRaises(HealthError):
            take_snapshot(self.root.path, clock=lambda: float("nan"))

    def test_reads_only_under_root_and_only_read_only(self) -> None:
        self.root.set_thermal({"thermal_zone0": 45000})
        self.root.set_machinecheck({"machinecheck0/bank0": "ff"})
        opened: list[tuple[str, object]] = []
        listed: list[str] = []
        real_open, real_listdir = builtins.open, os.listdir

        def spy_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
            opened.append((os.fspath(file), mode))
            return real_open(file, mode, *args, **kwargs)

        def spy_listdir(path="."):  # type: ignore[no-untyped-def]
            listed.append(os.fspath(path))
            return real_listdir(path)

        with mock.patch.object(builtins, "open", spy_open), mock.patch.object(os, "listdir", spy_listdir):
            take_snapshot(self.root.path)
        prefix = str(self.root.path)
        self.assertTrue(opened and listed)
        for name, mode in opened:
            self.assertTrue(name.startswith(prefix), name)
            self.assertEqual(mode, "rb", name)
        for name in listed:
            self.assertTrue(name.startswith(prefix), name)
        self.assertIn(str(self.root.tainted), [n for n, _ in opened])
        self.assertIn(str(self.root.modules_file), [n for n, _ in opened])
        self.assertIn(str(self.root.pci_dir), listed)

    def test_taking_a_snapshot_changes_nothing(self) -> None:
        def tree() -> dict[str, bytes | None]:
            return {str(p): (p.read_bytes() if p.is_file() else None) for p in sorted(self.root.path.rglob("*"))}

        before = tree()
        self.snap()
        self.assertEqual(tree(), before)


class SnapshotValidation(unittest.TestCase):
    def good(self, **kw: object) -> HealthSnapshot:
        args: dict[str, object] = dict(
            taken_at=1.0, taint=4, taint_bits=(2,), pci=frozenset({"a"}), modules=frozenset({"m"}), thermal_mc=None, machinecheck=None
        )
        args.update(kw)
        return HealthSnapshot(**args)  # type: ignore[arg-type]

    def test_accepts_consistent_values_and_is_frozen(self) -> None:
        s = self.good()
        with self.assertRaises(Exception):
            s.taint = 5  # type: ignore[misc]

    def test_rejects_inconsistent_or_malformed_values(self) -> None:
        bad = {
            "bits mismatch": dict(taint_bits=(3,)),
            "negative taint": dict(taint=-1, taint_bits=()),
            "bool taint": dict(taint=True, taint_bits=(0,)),
            "nan time": dict(taken_at=float("nan")),
            "str time": dict(taken_at="1"),
            "pci set": dict(pci={"a"}),
            "pci non-str": dict(pci=frozenset({1})),
            "modules list": dict(modules=["m"]),
            "thermal values": dict(thermal_mc={"z": "hot"}),
            "mc not mapping": dict(machinecheck=[1]),
        }
        for label, kw in bad.items():
            with self.subTest(label):
                with self.assertRaises(HealthError):
                    self.good(**kw)


class Persistence(RootCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.dir / "before.json"

    def test_round_trip(self) -> None:
        self.root.set_taint((1 << 29) | 4)
        self.root.set_thermal({"thermal_zone0": 45000})
        self.root.set_machinecheck({"machinecheck0/bank0": "ff"})
        original = self.snap()
        save_snapshot(original, self.path)
        self.assertEqual(load_snapshot(self.path), original)

    def test_round_trip_without_optional_data(self) -> None:
        original = self.snap()
        save_snapshot(original, self.path)
        loaded = load_snapshot(self.path)
        self.assertEqual(loaded, original)
        self.assertIsNone(loaded.thermal_mc)
        self.assertIsNone(loaded.machinecheck)

    def test_refuses_to_overwrite(self) -> None:
        self.path.write_bytes(b"precious\n")
        with self.assertRaises(HealthError) as cm:
            save_snapshot(self.snap(), self.path)
        self.assertIn("overwrite", str(cm.exception))
        self.assertEqual(self.path.read_bytes(), b"precious\n")
        save_snapshot(self.snap(), self.dir / "other.json")
        with self.assertRaises(HealthError):  # also not over its own earlier output
            save_snapshot(self.snap(), self.dir / "other.json")

    def test_json_is_sorted_newline_terminated_and_lf_only(self) -> None:
        self.root.set_thermal({"thermal_zone1": 1, "thermal_zone0": 2})
        self.root.set_modules(["zz", "aa", "mm"])
        save_snapshot(self.snap(), self.path)
        raw = self.path.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw.endswith(b"\n\n"))
        self.assertNotIn(b"\r", raw)
        doc = json.loads(raw)
        self.assertEqual(list(doc), sorted(doc))
        self.assertEqual(list(doc["thermal_mc"]), ["thermal_zone0", "thermal_zone1"])
        self.assertEqual(doc["modules"], ["aa", "mm", "zz"])
        self.assertEqual(doc["pci"], sorted(DEFAULT_PCI))
        self.assertEqual(doc["taint_bits"], [])
        self.assertEqual(doc["schema"], 1)

    def test_same_snapshot_same_bytes(self) -> None:
        s = self.snap()
        save_snapshot(s, self.dir / "a.json")
        save_snapshot(s, self.dir / "b.json")
        self.assertEqual((self.dir / "a.json").read_bytes(), (self.dir / "b.json").read_bytes())

    def test_save_into_a_missing_directory_is_an_error_and_creates_nothing(self) -> None:
        with self.assertRaises(HealthError):
            save_snapshot(self.snap(), self.dir / "nope" / "x.json")
        self.assertFalse((self.dir / "nope").exists())

    def test_save_uses_create_exclusive(self) -> None:
        real_open = os.open
        flags_seen: list[int] = []

        def spy(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            flags_seen.append(flags)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(os, "open", spy):
            save_snapshot(self.snap(), self.path)
        self.assertEqual(len(flags_seen), 1)
        self.assertTrue(flags_seen[0] & os.O_CREAT and flags_seen[0] & os.O_EXCL)

    def test_save_rejects_non_snapshots(self) -> None:
        with self.assertRaises(HealthError):
            save_snapshot({"taint": 0}, self.path)  # type: ignore[arg-type]
        self.assertFalse(self.path.exists())

    def test_load_rejects_bad_files(self) -> None:
        save_snapshot(self.snap(), self.path)
        good = json.loads(self.path.read_text())

        def written(doc: object, name: str) -> Path:
            p = self.dir / name
            p.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
            return p

        def variant(**changes: object) -> dict[str, object]:
            return {**good, **changes}

        bad = {
            "not json": "not json",
            "empty": "",
            "list": "[]",
            "nan time": '{"taken_at": NaN}',
            "missing key": {k: v for k, v in good.items() if k != "pci"},
            "extra key": variant(extra=1),
            "wrong schema": variant(schema=2),
            "bool schema": variant(schema=True),
            "taint str": variant(taint="0"),
            "negative taint": variant(taint=-4, taint_bits=[]),
            "bits mismatch": variant(taint=4, taint_bits=[3]),
            "bits not list": variant(taint_bits="2"),
            "pci not list": variant(pci="0000:00:00.0"),
            "pci item": variant(pci=[1]),
            "thermal values": variant(thermal_mc={"thermal_zone0": "hot"}),
            "thermal bool": variant(thermal_mc={"thermal_zone0": True}),
            "mc list": variant(machinecheck=[]),
            "taken_at str": variant(taken_at="now"),
        }
        for label, doc in bad.items():
            with self.subTest(label):
                with self.assertRaises(HealthError):
                    load_snapshot(written(doc, f"bad-{len(label)}-{label.replace(' ', '_')}.json"))

    def test_load_missing_or_undecodable_file_is_an_error(self) -> None:
        with self.assertRaises(HealthError):
            load_snapshot(self.dir / "absent.json")
        (self.dir / "binary.json").write_bytes(b"\xff\xfe{")
        with self.assertRaises(HealthError):
            load_snapshot(self.dir / "binary.json")

    def test_load_is_read_only(self) -> None:
        save_snapshot(self.snap(), self.path)
        before = self.path.read_bytes()
        load_snapshot(self.path)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()


class ProcfsReadSizeTest(unittest.TestCase):
    """Kernel 7.2 /proc/sys handlers fail a single 4 MiB read() with ENOMEM (measured lnx 2026-10-01)."""

    def test_no_single_read_exceeds_the_chunk_size(self) -> None:
        from arltuner.health import snapshot as snap

        class ProcLike:
            def __init__(self) -> None:
                self.data = b"12288\n"

            def read(self, n: int) -> bytes:
                if n > snap._READ_CHUNK_BYTES:
                    raise OSError(12, "Cannot allocate memory")
                out, self.data = self.data[:n], self.data[n:]
                return out

        self.assertEqual(snap._read_bounded(ProcLike()), b"12288\n")

    def test_oversize_is_still_detected(self) -> None:
        from arltuner.health import snapshot as snap

        class Endless:
            def read(self, n: int) -> bytes:
                return b"x" * n

        with mock.patch.object(snap, "_MAX_READ_BYTES", 100), mock.patch.object(snap, "_READ_CHUNK_BYTES", 7):
            self.assertEqual(len(snap._read_bounded(Endless())), 101)
