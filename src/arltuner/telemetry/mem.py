"""Live DDR5 frequency and timings from the memory controller's MCHBAR window (``mem.read``, contract 9). Read-only; facts in
``docs/knowledge/memory-mchbar.md`` (Intel Core Ultra 200S/200HX Series 2, document 835538 [DOC]; the fixture values [MEASURED lnx 2026-10-11]).

What this module may do: ``pread`` the host bridge's PCI config (MCHBAR register at ``0x48``), ``pread`` a **compiled whitelist** of exact (offset, width)
MMIO registers through ``/dev/mem`` (``O_RDONLY | O_SYNC``), read SMBIOS type 17 structures from ``/sys/firmware/dmi/entries`` and the ``spd5118`` hwmon
temperatures. No PCI or MMIO write, no MRC/PMA mailbox request, no MSR. Every failure yields ``{"available": False, "reason": ...}`` and no numbers.
All roots are injectable, so the tests run on synthetic files.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Any

from .readings import Reading, unavailable

__all__ = ["DEVMEM", "DMI_ROOT", "HWMON_ROOT", "INSTANCE_BASES", "MEM_WHITELIST", "PCI_CONFIG", "decode_instance", "parse_smbios17", "read_memory"]

PCI_CONFIG = Path("/sys/bus/pci/devices/0000:00:00.0/config")
DEVMEM = Path("/dev/mem")
DMI_ROOT = Path("/sys/firmware/dmi/entries")
HWMON_ROOT = Path("/sys/class/hwmon")

HOST_BRIDGE = (0x8086, 0x7D1A)  # Arrow Lake-S host bridge [MEASURED lnx 2026-10-11]
MCHBAR_REG = 0x48  # 8 bytes LE in the host bridge's config space; bit 0 = enable, base = value & ~0x7FFF
MCHBAR_WINDOW = 0x20000

#: MCHBAR-relative bases of the six timing instances. The *_broadcast ones read channel 0 of their controller (aliases, not extra data).
INSTANCE_BASES: dict[str, int] = {
    "mc0_channel0": 0x0E000, "mc0_channel1": 0x0E800, "mc0_broadcast": 0x0F000,
    "mc1_channel0": 0x1E000, "mc1_channel1": 0x1E800, "mc1_broadcast": 0x1F000,
}
#: Per-instance (relative offset, width) whitelist.
INSTANCE_REGS: tuple[tuple[int, int], ...] = ((0x000, 8), (0x00C, 4), (0x010, 4), (0x014, 4), (0x018, 4), (0x070, 4), (0x088, 8), (0x138, 8), (0x4A0, 8))
QCLK_REG, CROSS_REG, BCLK_REG, BW_REG = 0x13D10, 0x13D98, 0x5F60, 0x13D08
GLOBAL_REGS: tuple[tuple[int, int], ...] = ((QCLK_REG, 4), (CROSS_REG, 4), (BCLK_REG, 8), (BW_REG, 4))

#: The only (MCHBAR-relative offset, width) pairs this module will ever read from ``/dev/mem``.
MEM_WHITELIST: frozenset[tuple[int, int]] = frozenset([(base + rel, width) for base in INSTANCE_BASES.values() for rel, width in INSTANCE_REGS] + list(GLOBAL_REGS))

SOURCE = "mchbar"


class _Unavailable(Exception):
    pass


def _bits(value: int, hi: int, lo: int) -> int:
    return (value >> lo) & ((1 << (hi - lo + 1)) - 1)


# ---------------------------------------------------------------------------------------------------------------------------
# decode (pure)
# ---------------------------------------------------------------------------------------------------------------------------
def decode_instance(w: dict[int, int]) -> dict[str, Any] | None:
    """``w``: relative offset -> raw little-endian word (the :data:`INSTANCE_REGS` set). The fields per ``memory-mchbar.md`` "Field layout"; ``None`` when the
    plausibility check fails (``4 <= tCL <= 72``, ``8 <= tRCD <= 59``, ``10 <= tRP <= 76``, ``28 <= tRAS <= 136``, ``16 <= tFAW <= 88``, ``tRFC > 0``)."""
    pre, cas, gs, act, rf = w[0x000], w[0x070], w[0x088], w[0x138], w[0x4A0]
    t: dict[str, int] = {
        "tCL": _bits(cas, 22, 16), "tCWL": _bits(cas, 31, 24),
        "tRP": _bits(pre, 7, 0), "tRPab": _bits(pre, 17, 10), "tRTP": _bits(pre, 26, 20), "tRAS": _bits(pre, 53, 45),
        "tFAW": _bits(act, 8, 0), "tRRD_L": _bits(act, 14, 9), "tRRD_S": _bits(act, 21, 15), "tRCD": _bits(act, 29, 22), "tRCDW": _bits(act, 39, 32),
        "tREFI": _bits(rf, 17, 0), "tRFC": _bits(rf, 30, 18), "tRFCpb": _bits(rf, 50, 40), "tREFSBRD": _bits(rf, 58, 51),
    }
    twrpre = _bits(pre, 42, 33)
    t["tWR"] = twrpre - t["tCWL"] - 8  # DDR5 BL16
    t["tWTP"] = t["tCWL"] + 8 + t["tWR"]
    t["tRC"] = t["tRAS"] + t["tRP"]
    if not (4 <= t["tCL"] <= 72 and 8 <= t["tRCD"] <= 59 and 10 <= t["tRP"] <= 76 and 28 <= t["tRAS"] <= 136 and 16 <= t["tFAW"] <= 88 and t["tRFC"] > 0):
        return None
    if t["tWR"] < 0:
        return None
    turn = {
        "RDRD": {"SG": _bits(w[0x00C], 6, 0), "DG": _bits(w[0x00C], 14, 8), "SD": _bits(w[0x00C], 23, 16), "DD": _bits(w[0x00C], 31, 24)},
        "RDWR": {"SG": _bits(w[0x010], 7, 0), "DG": _bits(w[0x010], 15, 8), "SD": _bits(w[0x010], 23, 16), "DD": _bits(w[0x010], 31, 24)},
        "WRRD": {"SG": _bits(w[0x014], 8, 0), "DG": _bits(w[0x014], 17, 9), "SD": _bits(w[0x014], 24, 18), "DD": _bits(w[0x014], 31, 25)},
        "WRWR": {"SG": _bits(w[0x018], 6, 0), "DG": _bits(w[0x018], 14, 8), "SD": _bits(w[0x018], 22, 16), "DD": _bits(w[0x018], 31, 24)},
    }
    return {"timings": t, "turnaround": turn, "gear": 4 if _bits(gs, 31, 31) else 2, "cmd_rate": "2T" if _bits(gs, 3, 3) else "1T"}


def _decode_clocks(qclk: int, cross: int, bclk: int, bw: int) -> dict[str, Any]:
    ratio = _bits(qclk, 7, 0)
    if ratio == 0:
        raise _Unavailable("the trained QCLK ratio reads 0 (memory not trained / register not valid)")
    qclk_mhz = ratio * 100 / 3  # ratio x 33.333 MHz [DOC 835538]
    cpu_bclk_khz = _bits(bclk, 63, 32)
    bclk_mhz = cpu_bclk_khz / 1000 if cpu_bclk_khz else None
    cross_mhz: float | None = None
    agree: bool | None = None
    if cross != 0xFFFFFFFF and bclk_mhz:
        # 0x13D98 (empirical, memory-mchbar.md): ratio[19:12] x (4 if bit 24 else 2) x BCLK is the data rate in MT/s; QCLK = MT/s / 2. [UNVERIFIED reading of
        # the doc's "MHz": on the fixture ratio 36 x 2 x 100 = 7200 MT/s = 2 x 3600 MHz, which is how it "agrees"]
        cross_mhz = _bits(cross, 19, 12) * (4 if _bits(cross, 24, 24) else 2) * bclk_mhz / 2
        agree = abs(cross_mhz - qclk_mhz) <= 1.0
    return {
        "mts": round(2 * qclk_mhz), "qclk_mhz": round(qclk_mhz, 3), "bclk_mhz": round(bclk_mhz, 3) if bclk_mhz is not None else None,
        "cross_check": {"agree": agree, "mhz_a": round(qclk_mhz, 3), "mhz_b": round(cross_mhz, 3) if cross_mhz is not None else None},
        "max_bandwidth_mbs": _bits(bw, 28, 9),
    }


# ---------------------------------------------------------------------------------------------------------------------------
# raw access
# ---------------------------------------------------------------------------------------------------------------------------
class _Mmio:
    """``pread`` through ``/dev/mem`` at ``base + offset`` for whitelisted (offset, width) pairs only; anything else is a programming error."""

    def __init__(self, devmem: Path, base: int) -> None:
        flags = os.O_RDONLY | getattr(os, "O_SYNC", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            self._fd = os.open(devmem, flags)
        except OSError as exc:
            raise _Unavailable(f"cannot open {devmem}: {exc.strerror or exc} (errno {exc.errno})") from None
        self._base = base

    def close(self) -> None:
        os.close(self._fd)

    def read(self, offset: int, width: int) -> int:
        if (offset, width) not in MEM_WHITELIST:
            raise ValueError(f"MCHBAR+{offset:#x} width {width} is not in the compiled whitelist")
        try:
            data = os.pread(self._fd, width, self._base + offset)
        except OSError as exc:
            raise _Unavailable(f"read of MCHBAR+{offset:#x} failed: {exc.strerror or exc} (errno {exc.errno})") from None
        if len(data) != width:
            raise _Unavailable(f"short read of MCHBAR+{offset:#x}: {len(data)} of {width} bytes")
        return int.from_bytes(data, "little")


def _mchbar_base(pci_config: Path) -> int:
    try:
        fd = os.open(pci_config, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise _Unavailable(f"cannot open the host bridge config space {pci_config}: {exc.strerror or exc}") from None
    try:
        ident = os.pread(fd, 4, 0)
        reg = os.pread(fd, 8, MCHBAR_REG)
    except OSError as exc:
        raise _Unavailable(f"cannot read the host bridge config space: {exc.strerror or exc}") from None
    finally:
        os.close(fd)
    if len(ident) != 4 or len(reg) != 8:
        raise _Unavailable("short read of the host bridge config space")
    vendor, device = struct.unpack("<HH", ident)
    if (vendor, device) != HOST_BRIDGE:
        raise _Unavailable(f"unknown host bridge {vendor:04x}:{device:04x} (expected {HOST_BRIDGE[0]:04x}:{HOST_BRIDGE[1]:04x})")
    value = int.from_bytes(reg, "little")
    if not value & 1:
        raise _Unavailable("MCHBAR is disabled (enable bit clear)")
    base = value & ~0x7FFF & 0xFFFFFFFFFFFFFFFF
    if base == 0:
        raise _Unavailable("MCHBAR base is 0")
    return base


# ---------------------------------------------------------------------------------------------------------------------------
# DIMMs: SMBIOS type 17 + spd5118
# ---------------------------------------------------------------------------------------------------------------------------
def _strings(raw: bytes, length: int) -> list[str]:
    out: list[str] = []
    for chunk in raw[length:].split(b"\x00"):
        if not chunk:
            break
        out.append(chunk.decode("ascii", "replace").strip())
    return out


def parse_smbios17(raw: bytes) -> dict[str, Any] | None:
    """One SMBIOS type 17 (Memory Device) structure as ``/sys/firmware/dmi/entries/17-*/raw`` holds it (formatted area, then the string set). Offsets: SMBIOS
    3.x spec section 7.18: size ``0x0C`` (0 = empty slot, ``0x7FFF`` = extended size at ``0x1C``), device locator ``0x10``, speed ``0x15``, manufacturer
    ``0x17``, part number ``0x1A``, attributes ``0x1B`` (rank in bits 3:0), configured speed ``0x20``, configured voltage ``0x26``. ``None`` for an empty or
    malformed slot."""
    if len(raw) < 0x1B or raw[0] != 17 or raw[1] < 0x1B or raw[1] > len(raw):
        return None
    length = raw[1]
    strings = _strings(raw, length)

    def text(index: int) -> str:
        return strings[raw[index] - 1] if 0 < raw[index] <= len(strings) else ""

    def u16(offset: int) -> int | None:
        return struct.unpack_from("<H", raw, offset)[0] if length >= offset + 2 else None

    size_word = u16(0x0C) or 0
    if size_word == 0:
        return None  # no module installed
    if size_word == 0xFFFF:
        size_mb: int | None = None  # size unknown
    elif size_word == 0x7FFF:
        ext = struct.unpack_from("<I", raw, 0x1C)[0] & 0x7FFFFFFF if length >= 0x20 else 0
        size_mb = ext or None
    else:
        size_mb = (size_word & 0x7FFF) // 1024 if size_word & 0x8000 else size_word  # bit 15: KB units
    speed, configured = u16(0x15), u16(0x20)
    voltage_mv = u16(0x26)
    return {
        "locator": text(0x10),
        "size": size_mb,  # MB (MiB as SMBIOS counts them), None = unknown
        "part": text(0x1A),
        "manufacturer": text(0x17),
        "speed": speed if speed not in (None, 0, 0xFFFF) else None,  # MT/s
        "configured_speed": configured if configured not in (None, 0, 0xFFFF) else None,  # MT/s
        "voltage": voltage_mv / 1000 if voltage_mv not in (None, 0, 0xFFFF) else None,  # V
        "rank": (raw[0x1B] & 0x0F) or None if length > 0x1B else None,
    }


def _dimm_structs(dmi_root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        entries = sorted(dmi_root.glob("17-*"), key=lambda p: (len(p.name), p.name))
    except OSError:
        return out
    for entry in entries:
        try:
            parsed = parse_smbios17((entry / "raw").read_bytes()[:4096])
        except OSError:
            continue
        if parsed is not None:
            out.append(parsed)
    return out


def _spd_temps(hwmon_root: Path) -> list[tuple[str, Reading]]:
    """``spd5118`` hwmon devices in bus-address order: (source label, reading). A sensor that cannot be read is an unavailable reading, never a number."""
    found: list[tuple[tuple[int, str], str, Reading]] = []
    try:
        hwmons = sorted(hwmon_root.glob("hwmon*"))
    except OSError:
        return []
    for hw in hwmons:
        try:
            if (hw / "name").read_text(encoding="ascii").strip() != "spd5118":
                continue
        except OSError:
            continue
        try:
            addr = os.path.basename(os.path.realpath(hw / "device"))  # e.g. "6-0050": the SPD hub's i2c bus-address
        except OSError:
            addr = ""
        source = f"hwmon:{hw.name}:spd5118" + (f":{addr}" if addr and addr != "device" else "")
        number = "".join(ch for ch in hw.name if ch.isdigit())
        try:
            milli = int((hw / "temp1_input").read_text(encoding="ascii").strip())
            reading = Reading(round(milli / 1000, 1), "C", source)
        except (OSError, ValueError) as exc:
            reading = unavailable(source, f"temp1_input unreadable: {exc}", "C")
        found.append(((int(number or 0), hw.name), source, reading))
    found.sort(key=lambda item: item[0])
    return [(src, r) for _, src, r in found]


def _dimms(dmi_root: Path, hwmon_root: Path) -> list[dict[str, Any]]:
    dimms = _dimm_structs(dmi_root)
    sensors = _spd_temps(hwmon_root)
    # The i2c address order of the SPD hubs is taken to follow the SMBIOS slot order [UNVERIFIED]; with a different count the mapping is not attempted.
    mapped = len(sensors) == len(dimms)
    for i, dimm in enumerate(dimms):
        if mapped:
            reading = sensors[i][1]
        elif not sensors:
            reading = unavailable("hwmon:spd5118", "no spd5118 hwmon device", "C")
        else:
            reading = unavailable("hwmon:spd5118", f"{len(sensors)} spd5118 sensor(s) for {len(dimms)} DIMM(s): cannot tell which is which", "C")
        dimm["temp_c"] = reading.to_json()
    return dimms


# ---------------------------------------------------------------------------------------------------------------------------
# the op
# ---------------------------------------------------------------------------------------------------------------------------
def read_memory(
    *, pci_config: Path | str = PCI_CONFIG, devmem: Path | str = DEVMEM, dmi_root: Path | str = DMI_ROOT, hwmon_root: Path | str = HWMON_ROOT
) -> dict[str, Any]:
    """The ``mem.read`` result (contract 9). Needs root on hardware (config space + ``/dev/mem``); any failure gives ``{"available": False, "reason": ...}``."""
    try:
        base = _mchbar_base(Path(pci_config))
        mmio = _Mmio(Path(devmem), base)
        try:
            instances: list[tuple[tuple[int, ...], dict[str, Any]]] = []
            for inst_base in INSTANCE_BASES.values():
                words = {rel: mmio.read(inst_base + rel, width) for rel, width in INSTANCE_REGS}
                decoded = decode_instance(words)
                if decoded is not None:
                    instances.append((tuple(words[rel] for rel, _ in INSTANCE_REGS), decoded))
            if not instances:
                raise _Unavailable("no memory controller instance passes the plausibility check")
            raw0, chosen = instances[0]  # the first plausible instance is the reported one
            agree = sum(1 for raw, _ in instances if raw == raw0)
            clocks = _decode_clocks(mmio.read(QCLK_REG, 4), mmio.read(CROSS_REG, 4), mmio.read(BCLK_REG, 8), mmio.read(BW_REG, 4))
        finally:
            mmio.close()
    except _Unavailable as exc:
        return {"available": False, "reason": str(exc)}
    return {
        "available": True,
        "mts": clocks["mts"], "qclk_mhz": clocks["qclk_mhz"], "gear": chosen["gear"], "cmd_rate": chosen["cmd_rate"], "bclk_mhz": clocks["bclk_mhz"],
        "timings": chosen["timings"], "turnaround": chosen["turnaround"],
        "instances_ok": agree, "cross_check": clocks["cross_check"], "max_bandwidth_mbs": clocks["max_bandwidth_mbs"],
        "dimms": _dimms(Path(dmi_root), Path(hwmon_root)),
    }
