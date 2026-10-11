"""``mem.read`` decode (docs/knowledge/memory-mchbar.md): synthetic config space, /dev/mem and DMI files built from the measured fixture values."""
from __future__ import annotations

import os
import struct
import tempfile
import unittest
from pathlib import Path

from arltuner.telemetry import mem

BASE = 0x40000  # a small, 32 KiB-aligned fake MCHBAR so the fake /dev/mem file stays tiny


def put(word: int, value: int, hi: int, lo: int) -> int:
    assert value < (1 << (hi - lo + 1)), (value, hi, lo)
    return word | (value << lo)


# the reference machine, BIOS DDR5-7200 [MEASURED lnx 2026-10-11]
FIX = dict(tCL=36, tRCD=44, tRP=44, tRAS=96, tRC=140, tRFC=574, tFAW=32, tCWL=34, tRTP=23, tWR=110, tWRPRE=152, tRRD_L=12, tRRD_S=8, tREFI=32767,
           tRCDW=44, tRPab=44, tRFCpb=467, tREFSBRD=40)
TURN = {"RDRD": (18, 8, 16, 16), "WRWR": (72, 8, 18, 18), "RDWR": (23, 23, 23, 23), "WRRD": (68, 48, 13, 13)}


def instance_words(fix: dict[str, int] = FIX, *, gear4: bool = False, stretch: bool = True) -> dict[int, int]:
    pre = 0
    for v, hi, lo in ((fix["tRP"], 7, 0), (fix["tRPab"], 17, 10), (fix["tRTP"], 26, 20), (fix["tWRPRE"], 42, 33), (fix["tRAS"], 53, 45)):
        pre = put(pre, v, hi, lo)
    act = 0
    for v, hi, lo in ((fix["tFAW"], 8, 0), (fix["tRRD_L"], 14, 9), (fix["tRRD_S"], 21, 15), (fix["tRCD"], 29, 22), (fix["tRCDW"], 39, 32)):
        act = put(act, v, hi, lo)
    rf = 0
    for v, hi, lo in ((fix["tREFI"], 17, 0), (fix["tRFC"], 30, 18), (fix["tRFCpb"], 50, 40), (fix["tREFSBRD"], 58, 51)):
        rf = put(rf, v, hi, lo)
    cas = put(put(0, fix["tCL"], 22, 16), fix["tCWL"], 31, 24)
    gs = put(0, 1 if gear4 else 0, 31, 31) | ((1 << 3) if stretch else 0)
    fields = {"RDRD": ((6, 0), (14, 8), (23, 16), (31, 24)), "RDWR": ((7, 0), (15, 8), (23, 16), (31, 24)),
              "WRRD": ((8, 0), (17, 9), (24, 18), (31, 25)), "WRWR": ((6, 0), (14, 8), (22, 16), (31, 24))}
    turn = {}
    for name, layout in fields.items():
        w = 0
        for value, (hi, lo) in zip(TURN[name], layout):
            w = put(w, value, hi, lo)
        turn[name] = w
    return {0x000: pre, 0x00C: turn["RDRD"], 0x010: turn["RDWR"], 0x014: turn["WRRD"], 0x018: turn["WRWR"], 0x070: cas, 0x088: gs, 0x138: act, 0x4A0: rf}


class Fake:
    """A temp directory holding config space, a sparse /dev/mem, dmi entries and hwmon."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.config = tmp / "config"
        self.devmem = tmp / "devmem"
        self.dmi = tmp / "dmi"
        self.hwmon = tmp / "hwmon"
        self.dmi.mkdir()
        self.hwmon.mkdir()
        self.write_config()
        self.devmem.write_bytes(b"")
        for inst in mem.INSTANCE_BASES.values():
            self.put_instance(inst, instance_words())
        self.put(mem.QCLK_REG, 108 | (0 << 8), 4)  # ratio 108 x 33.333 = 3600 MHz
        self.put(mem.CROSS_REG, 36 << 12, 4)  # 36 x 2 x 100 = 7200 MT/s
        self.put(mem.BCLK_REG, (100000 << 32) | 100004, 8)
        self.put(mem.BW_REG, 115200 << 9, 4)

    def write_config(self, *, vendor: int = 0x8086, device: int = 0x7D1A, reg: int | None = None, short: bool = False) -> None:
        data = bytearray(256)
        struct.pack_into("<HH", data, 0, vendor, device)
        struct.pack_into("<Q", data, 0x48, (BASE | 1) if reg is None else reg)
        self.config.write_bytes(bytes(data[:0x4C] if short else data))

    def put(self, offset: int, value: int, width: int) -> None:
        with open(self.devmem, "r+b") as f:
            f.seek(BASE + offset)
            f.write(value.to_bytes(width, "little"))

    def put_instance(self, base: int, words: dict[int, int]) -> None:
        for (rel, width) in mem.INSTANCE_REGS:
            self.put(base + rel, words[rel], width)

    def read(self) -> dict:
        return mem.read_memory(pci_config=self.config, devmem=self.devmem, dmi_root=self.dmi, hwmon_root=self.hwmon)


def smbios17(*, locator: str = "DIMM_A1", size: int = 16384, part: str = "KF572C40-16", maker: str = "Kingston", speed: int = 7200, configured: int = 7200,
             volt_mv: int = 1400, rank: int = 1, size_word: int | None = None, ext: int = 0, length: int = 0x28) -> bytes:
    head = bytearray(length)
    head[0], head[1] = 17, length
    struct.pack_into("<H", head, 0x0C, size if size_word is None else size_word)
    head[0x10] = 1
    head[0x11] = 2
    head[0x12] = 0x22
    struct.pack_into("<H", head, 0x15, speed)
    head[0x17] = 3
    head[0x1A] = 4
    head[0x1B] = rank
    struct.pack_into("<I", head, 0x1C, ext)
    struct.pack_into("<H", head, 0x20, configured)
    struct.pack_into("<H", head, 0x26, volt_mv)
    strings = b"".join(s.encode() + b"\x00" for s in (locator, "BANK 0", maker, part)) + b"\x00"
    return bytes(head) + strings


class MemTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.f = Fake(Path(tmp.name))

    def test_decode_equals_the_fixture(self) -> None:
        r = self.f.read()
        self.assertTrue(r["available"], r)
        self.assertEqual((r["mts"], r["qclk_mhz"], r["gear"], r["cmd_rate"], r["bclk_mhz"]), (7200, 3600.0, 2, "2T", 100.0))
        t = r["timings"]
        for name in ("tCL", "tRCD", "tRP", "tRAS", "tRC", "tRFC", "tFAW", "tCWL", "tRTP", "tWR", "tRRD_L", "tRRD_S", "tREFI", "tRCDW", "tRPab", "tRFCpb"):
            self.assertEqual(t[name], FIX[name], name)
        self.assertEqual(t["tWTP"], 34 + 8 + 110)
        self.assertEqual({k: tuple(v[x] for x in ("SG", "DG", "SD", "DD")) for k, v in r["turnaround"].items()}, TURN)
        self.assertEqual((r["instances_ok"], r["max_bandwidth_mbs"]), (6, 115200))
        self.assertEqual(r["cross_check"], {"agree": True, "mhz_a": 3600.0, "mhz_b": 3600.0})
        self.assertEqual(set(t), {"tCL", "tRCD", "tRP", "tRAS", "tRC", "tRFC", "tRFCpb", "tREFI", "tFAW", "tRRD_L", "tRRD_S", "tCWL", "tRTP", "tWR", "tWTP", "tRCDW", "tRPab", "tREFSBRD"})

    def test_gear4_and_1t(self) -> None:
        for inst in mem.INSTANCE_BASES.values():
            self.f.put_instance(inst, instance_words(gear4=True, stretch=False))
        r = self.f.read()
        self.assertEqual((r["gear"], r["cmd_rate"]), (4, "1T"))

    def test_instance_agreement_counts_identical_words_only(self) -> None:
        other = dict(FIX, tFAW=40)
        self.f.put_instance(mem.INSTANCE_BASES["mc1_channel1"], instance_words(other))
        self.assertEqual(self.f.read()["instances_ok"], 5)

    def test_an_implausible_first_instance_is_skipped(self) -> None:
        self.f.put_instance(mem.INSTANCE_BASES["mc0_channel0"], instance_words(dict(FIX, tCL=2)))
        r = self.f.read()
        self.assertTrue(r["available"])
        self.assertEqual((r["timings"]["tCL"], r["instances_ok"]), (36, 5))

    def test_every_instance_implausible_is_unavailable(self) -> None:
        for inst in mem.INSTANCE_BASES.values():
            self.f.put_instance(inst, instance_words(dict(FIX, tRFC=0)))
        r = self.f.read()
        self.assertEqual(set(r), {"available", "reason"})
        self.assertFalse(r["available"])

    def test_cross_check_disagree_and_invalid(self) -> None:
        self.f.put(mem.CROSS_REG, 40 << 12, 4)
        self.assertEqual(self.f.read()["cross_check"]["agree"], False)
        self.f.put(mem.CROSS_REG, 0xFFFFFFFF, 4)
        cc = self.f.read()["cross_check"]
        self.assertEqual((cc["agree"], cc["mhz_b"]), (None, None))

    def assertUnavailable(self, r: dict, text: str) -> None:
        self.assertEqual(set(r), {"available", "reason"}, r)
        self.assertFalse(r["available"])
        self.assertIn(text, r["reason"])

    def test_bad_host_bridge(self) -> None:
        self.f.write_config(device=0x1234)
        self.assertUnavailable(self.f.read(), "unknown host bridge 8086:1234")

    def test_mchbar_disabled_or_zero(self) -> None:
        self.f.write_config(reg=BASE)  # enable bit clear
        self.assertUnavailable(self.f.read(), "disabled")
        self.f.write_config(reg=1)
        self.assertUnavailable(self.f.read(), "base is 0")

    def test_short_config_space(self) -> None:
        self.f.write_config(short=True)
        self.assertUnavailable(self.f.read(), "short read")

    def test_short_mmio_read(self) -> None:
        self.f.devmem.write_bytes(b"\x00" * (BASE + 0x0E000 + 4))  # ends in the middle of the first instance's first register
        self.assertUnavailable(self.f.read(), "short read")

    def test_missing_devmem_and_config(self) -> None:
        self.f.devmem.unlink()
        self.assertUnavailable(self.f.read(), "cannot open")
        self.f.config.unlink()
        self.assertUnavailable(self.f.read(), "host bridge config space")

    def test_the_reader_refuses_anything_outside_the_whitelist(self) -> None:
        m = mem._Mmio(self.f.devmem, BASE)
        self.addCleanup(m.close)
        self.assertEqual(m.read(mem.QCLK_REG, 4) & 0xFF, 108)
        for offset, width in ((0x0E000, 4), (0x0E000 + 0x001, 8), (0x20, 4), (mem.QCLK_REG, 8), (0x13D14, 4)):
            with self.assertRaises(ValueError, msg=(offset, width)):
                m.read(offset, width)
        self.assertEqual(len(mem.MEM_WHITELIST), 6 * 9 + 4)

    def test_no_write_path_exists(self) -> None:
        src = Path(mem.__file__).read_text(encoding="utf-8")
        for needle in ("O_RDWR", "O_WRONLY", "os.write", "pwrite", "wrmsr", "subprocess", "mmap"):
            self.assertNotIn(needle, src)

    def test_dimms_from_smbios_and_spd5118(self) -> None:
        for n, (loc, size) in enumerate((("DIMM_A1", 16384), ("DIMM_B1", 16384))):
            d = self.f.dmi / f"17-{n}"
            d.mkdir()
            (d / "raw").write_bytes(smbios17(locator=loc, size=size))
        empty = self.f.dmi / "17-2"
        empty.mkdir()
        (empty / "raw").write_bytes(smbios17(size=0))
        for n, milli in ((5, 41250), (6, 40000)):
            hw = self.f.hwmon / f"hwmon{n}"
            hw.mkdir()
            (hw / "name").write_text("spd5118\n")
            (hw / "temp1_input").write_text(f"{milli}\n")
        other = self.f.hwmon / "hwmon1"
        other.mkdir()
        (other / "name").write_text("coretemp\n")
        dimms = self.f.read()["dimms"]
        self.assertEqual([d["locator"] for d in dimms], ["DIMM_A1", "DIMM_B1"])  # the empty slot is not a DIMM
        d = dimms[0]
        self.assertEqual((d["size"], d["part"], d["manufacturer"], d["speed"], d["configured_speed"], d["voltage"], d["rank"]),
                         (16384, "KF572C40-16", "Kingston", 7200, 7200, 1.4, 1))
        self.assertEqual((d["temp_c"]["value"], d["temp_c"]["status"], d["temp_c"]["unit"]), (41.2, "ok", "C"))
        self.assertEqual(dimms[1]["temp_c"]["value"], 40.0)

    def test_sensor_count_mismatch_is_unavailable_not_guessed(self) -> None:
        d = self.f.dmi / "17-0"
        d.mkdir()
        (d / "raw").write_bytes(smbios17())
        d = self.f.dmi / "17-1"
        d.mkdir()
        (d / "raw").write_bytes(smbios17())
        hw = self.f.hwmon / "hwmon5"
        hw.mkdir()
        (hw / "name").write_text("spd5118\n")
        (hw / "temp1_input").write_text("41000\n")
        for dimm in self.f.read()["dimms"]:
            self.assertEqual((dimm["temp_c"]["value"], dimm["temp_c"]["status"]), (None, "unavailable"))
            self.assertIn("cannot tell", dimm["temp_c"]["reason"])

    def test_no_sensors_is_unavailable(self) -> None:
        d = self.f.dmi / "17-0"
        d.mkdir()
        (d / "raw").write_bytes(smbios17())
        self.assertEqual(self.f.read()["dimms"][0]["temp_c"]["status"], "unavailable")

    def test_smbios_size_encodings(self) -> None:
        self.assertEqual(mem.parse_smbios17(smbios17(size_word=0x7FFF, ext=65536))["size"], 65536)  # extended size
        self.assertEqual(mem.parse_smbios17(smbios17(size_word=0x8000 | 2048))["size"], 2)  # KB units
        self.assertIsNone(mem.parse_smbios17(smbios17(size_word=0xFFFF))["size"])  # unknown
        self.assertIsNone(mem.parse_smbios17(smbios17(size=0)))  # empty slot
        self.assertIsNone(mem.parse_smbios17(b"\x11\x05\x00"))  # truncated
        self.assertIsNone(mem.parse_smbios17(b"\x10" + bytes(60)))  # not type 17
        self.assertEqual(mem.parse_smbios17(smbios17(volt_mv=0))["voltage"], None)

    def test_isolation_imports(self) -> None:
        import ast

        tree = ast.parse(Path(mem.__file__).read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertFalse({m for m in names if m and m.startswith(("arltuner.mailbox", "arltuner.guard", "arltuner.msr"))})


if __name__ == "__main__":
    unittest.main()
