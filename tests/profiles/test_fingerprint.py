"""G-138: ``fingerprint.json`` (``arltuner.profiles.fingerprint``): the record the Windows restore finds the same cores with. Pure data, no hardware."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arltuner.profiles.fingerprint import (
    FINGERPRINT_FILE,
    CpuId,
    FingerprintError,
    build_fingerprint,
    cpu_id_from_cpuinfo,
    load_fingerprint,
    parse_fingerprint,
)
from arltuner.topology import arl_s_285k, synthetic

from tests.fixtures.win_reference import FUSED_POINT, REFERENCE_CPU_ID, reference_fingerprint

CPUINFO = "processor\t: 0\nvendor_id\t: GenuineIntel\ncpu family\t: 6\nmodel\t\t: 198\nmodel name\t: Intel(R)  Core(TM) Ultra 9 285K\nstepping\t: 2\n\nprocessor\t: 1\nmodel name\t: other\n"


class Roundtrip(unittest.TestCase):
    def test_json_shape_and_roundtrip(self) -> None:
        fp = reference_fingerprint()
        doc = json.loads(fp.to_json("2026-10-12T00:00:00Z"))
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["recorded"], "2026-10-12T00:00:00Z")
        self.assertEqual(doc["cpu"], {"vendor": "GenuineIntel", "family": 6, "model": 198, "brand": REFERENCE_CPU_ID.brand})
        self.assertEqual(doc["cpus"][0], {"cpu": 0, "kind": "P", "index": 0, "cluster": None, "favored": True, "fused_word": "0x5B439"})
        self.assertEqual(doc["cpus"][12], {"cpu": 12, "kind": "E", "index": 4, "cluster": 1, "favored": False, "fused_word": "0x48C2E"})
        back = parse_fingerprint(fp.to_json("t"))
        self.assertEqual(back, fp)
        self.assertEqual(back.topology(), arl_s_285k())

    def test_another_shape_roundtrips_to_its_own_topology(self) -> None:
        topo = synthetic(6, 8)
        words = {c: 0x50000 + 0x100 * c for c in topo.cpus_of_kind("P")} | {c: 0x48C2E for c in topo.cpus_of_kind("E")}
        fp = build_fingerprint(topo, words, CpuId("GenuineIntel", 6, 1, "Some CPU"))
        self.assertEqual(parse_fingerprint(fp.to_json("t")).require_restorable(), topo)


class Restorable(unittest.TestCase):
    def refused(self, fp, needle: str) -> None:  # type: ignore[no-untyped-def]
        with self.assertRaises(FingerprintError) as cm:
            fp.require_restorable()
        self.assertIn(needle, str(cm.exception))

    def test_the_reference_fingerprint_is_restorable(self) -> None:
        self.assertEqual(reference_fingerprint().require_restorable(), arl_s_285k())

    def test_missing_identity_or_words_say_how_to_fix_it(self) -> None:
        self.refused(reference_fingerprint(cpu_id=None), "no CPU identity")
        self.refused(reference_fingerprint({c: w for c, w in FUSED_POINT.items() if c != 5}), "P-core CPU 5")
        self.refused(reference_fingerprint({c: w for c, w in FUSED_POINT.items() if c not in (12, 13, 14, 15)}), "E cluster 1")
        self.refused(reference_fingerprint({}), "re-accept on Linux")

    def test_duplicate_p_words_cannot_be_told_apart(self) -> None:
        self.refused(reference_fingerprint({**FUSED_POINT, 6: FUSED_POINT[1]}), "cannot be told apart")

    def test_an_e_cluster_with_two_words_is_refused(self) -> None:
        self.refused(reference_fingerprint({**FUSED_POINT, 13: 0x1234}), "E cluster 1")


class Parse(unittest.TestCase):
    def test_bad_documents_are_refused_with_the_hint(self) -> None:
        good = json.loads(reference_fingerprint().to_json("t"))
        bad = {
            "not json": "{",
            "not an object": "[]",
            "other schema": json.dumps({**good, "schema": 2}),
            "no cpus": json.dumps({**good, "cpus": []}),
            "bad kind": json.dumps({**good, "cpus": [{**good["cpus"][0], "kind": "X"}]}),
            "P with a cluster": json.dumps({**good, "cpus": [{**good["cpus"][0], "cluster": 0}]}),
            "E without a cluster": json.dumps({**good, "cpus": [{**good["cpus"][12], "cluster": None}]}),
            "bad word": json.dumps({**good, "cpus": [{**good["cpus"][0], "fused_word": "5B439"}]}),
            "bool cpu": json.dumps({**good, "cpus": [{**good["cpus"][0], "cpu": True}]}),
            "duplicate cpu": json.dumps({**good, "cpus": [good["cpus"][0], good["cpus"][0]]}),
        }
        for name, text in bad.items():
            with self.subTest(name), self.assertRaises(FingerprintError):
                parse_fingerprint(text)

    def test_load_missing_file_says_re_accept(self) -> None:
        with tempfile.TemporaryDirectory() as t, self.assertRaises(FingerprintError) as cm:
            load_fingerprint(Path(t) / FINGERPRINT_FILE)
        self.assertIn("re-accept on Linux to record the fingerprint", str(cm.exception))

    def test_load_reads_a_file_and_refuses_a_huge_one(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / FINGERPRINT_FILE
            path.write_text(reference_fingerprint().to_json("t"), encoding="utf-8")
            self.assertEqual(load_fingerprint(path), reference_fingerprint())
            path.write_text(" " * (300 * 1024), encoding="utf-8")
            with self.assertRaises(FingerprintError):
                load_fingerprint(path)


class CpuInfo(unittest.TestCase):
    def test_first_processor_block_and_whitespace(self) -> None:
        self.assertEqual(cpu_id_from_cpuinfo(CPUINFO), CpuId("GenuineIntel", 6, 198, "Intel(R) Core(TM) Ultra 9 285K"))

    def test_incomplete_cpuinfo_gives_none(self) -> None:
        self.assertIsNone(cpu_id_from_cpuinfo(""))
        self.assertIsNone(cpu_id_from_cpuinfo("processor : 0\nvendor_id : GenuineIntel\n"))
        self.assertIsNone(cpu_id_from_cpuinfo(CPUINFO.replace("198", "x")))

    def test_mismatch_texts(self) -> None:
        self.assertIsNone(REFERENCE_CPU_ID.mismatch(CpuId("GenuineIntel", 6, 198, "  Intel(R) Core(TM)   Ultra 9 285K")))
        self.assertIn("model", REFERENCE_CPU_ID.mismatch(CpuId("GenuineIntel", 6, 1, REFERENCE_CPU_ID.brand)) or "")


if __name__ == "__main__":
    unittest.main()
